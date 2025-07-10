# app.py
import os
import sqlite3
import logging
import gc
from datetime import datetime, timezone, timedelta
import feedparser
import aiohttp
import asyncio
from flask import Flask, render_template, request, redirect, url_for, flash
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.jobstores.base import JobLookupError
from bs4 import BeautifulSoup

try:
    from bark_sender import send_bark_notification
except ImportError:
    print("错误: 无法导入 bark_sender.py。请确保该文件存在于同一目录下。")
    def send_bark_notification(device_key, body, title="", **kwargs):
        print(f"[DUMMY BARK] To: {device_key}, Title: {title}, Body: {body}")
        print(f"Other args: {kwargs}")
        return True, {"messageid": "dummy_id", "code": 200, "message": "Dummy success"}

try:
    from google import genai
    from google.genai import types
except ImportError:
    print("错误: 无法导入 google.genai。请确保已安装：pip install -q -U google-genai ")
    genai = None
    types = None

from database import (
    get_db_connection, init_db, cleanup_old_feed_items, DATABASE_FILE, _db_lock,
    get_detailed_feed_items_for_summary,
    get_all_keyword_triggers, add_keyword_trigger, delete_keyword_trigger
)

APP_SECRET_KEY = os.environ.get('APP_SECRET_KEY', os.urandom(24))
FEED_REQUEST_TIMEOUT = int(os.environ.get('FEED_REQUEST_TIMEOUT', 20))

# --- 北京时区定义 ---
BEIJING_TZ = timezone(timedelta(hours=8), 'Asia/Shanghai')

# --- 日志配置 (使用北京时间) ---
LOG_LEVEL_STR = os.environ.get('LOG_LEVEL', 'INFO').upper()
LOG_LEVEL = getattr(logging, LOG_LEVEL_STR, logging.INFO)

class BeijingTimeFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt_beijing = datetime.fromtimestamp(record.created, BEIJING_TZ)
        if datefmt:
            s = dt_beijing.strftime(datefmt)
        else:
            s = dt_beijing.strftime('%Y-%m-%d %H:%M:%S')
            s += f',{int(record.msecs):03d} CST'
        return s

logger_format_string = '%(asctime)s - %(levelname)s - %(name)s - %(message)s'
logger_date_format_string = '%Y-%m-%d %H:%M:%S'
beijing_formatter = BeijingTimeFormatter(fmt=logger_format_string, datefmt=logger_date_format_string)

root_logger = logging.getLogger()
root_logger.setLevel(LOG_LEVEL)
for handler in root_logger.handlers[:]:
    root_logger.removeHandler(handler)
console_handler = logging.StreamHandler()
console_handler.setFormatter(beijing_formatter)
root_logger.addHandler(console_handler)

logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = APP_SECRET_KEY

# --- Jinja2 过滤器 ---
def format_datetime_to_beijing_time(dt_obj, fmt='%Y-%m-%d %H:%M:%S'):
    if dt_obj is None:
        return '从未'
    if not isinstance(dt_obj, datetime):
        return str(dt_obj)
    if dt_obj.tzinfo is None:
        dt_obj = dt_obj.replace(tzinfo=timezone.utc)
    beijing_dt = dt_obj.astimezone(BEIJING_TZ)
    return beijing_dt.strftime(fmt)

app.jinja_env.filters['beijing_time'] = format_datetime_to_beijing_time

scheduler = BackgroundScheduler(
    timezone=timezone.utc,
    executors={'default': {'type': 'threadpool', 'max_workers': 3}}
)

# --- 数据库操作函数 (订阅相关) ---
def add_sub_to_db(name, url, interval_minutes, bark_key):
    try:
        with get_db_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO subscriptions (name, url, interval_minutes, bark_key, is_active, created_at, last_checked_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, url, interval_minutes, bark_key, True, datetime.now(timezone.utc), None)
            )
            conn.commit()
            return cursor.lastrowid
    except sqlite3.IntegrityError:
        logger.warning(f"尝试添加已存在的URL: {url}")
        return None
    except sqlite3.Error as e:
        logger.error(f"添加订阅到数据库时发生错误: {e}")
        return None

def get_all_subscriptions():
    try:
        with get_db_connection() as conn:
            subs = conn.execute("SELECT * FROM subscriptions ORDER BY created_at DESC").fetchall()
            return subs
    except sqlite3.Error as e:
        logger.error(f"获取所有订阅时发生数据库错误: {e}")
        return []

def get_subscription_by_id(sub_id):
    try:
        with get_db_connection() as conn:
            sub = conn.execute("SELECT * FROM subscriptions WHERE id = ?", (sub_id,)).fetchone()
            return sub
    except sqlite3.Error as e:
        logger.error(f"获取 ID 为 {sub_id} 的订阅时发生数据库错误: {e}")
        return None

def update_subscription_last_item(sub_id, item_guid):
    try:
        with get_db_connection() as conn:
            conn.execute(
                "UPDATE subscriptions SET last_fetched_item_guid = ?, last_checked_at = ? WHERE id = ?",
                (item_guid, datetime.now(timezone.utc), sub_id)
            )
            conn.commit()
    except sqlite3.Error as e:
        logger.error(f"更新订阅 {sub_id} 的 last_item 时发生数据库错误: {e}")

def update_subscription_last_checked(sub_id):
    try:
        with get_db_connection() as conn:
            conn.execute(
                "UPDATE subscriptions SET last_checked_at = ? WHERE id = ?",
                (datetime.now(timezone.utc), sub_id)
            )
            conn.commit()
    except sqlite3.Error as e:
        logger.error(f"更新订阅 {sub_id} 的 last_checked_at 时发生数据库错误: {e}")

def delete_sub_from_db(sub_id):
    with _db_lock:
        try:
            with get_db_connection() as conn:
                existing = conn.execute("SELECT name FROM subscriptions WHERE id = ?", (sub_id,)).fetchone()
                if not existing:
                    logger.warning(f"尝试删除不存在的订阅 ID: {sub_id}")
                    return False
                conn.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
                conn.commit()
                logger.info(f"已从数据库删除订阅 ID: {sub_id} (名称: {existing['name']})")
                return True
        except sqlite3.Error as e:
            logger.error(f"删除订阅 {sub_id} 时发生数据库错误: {e}")
            return False

def toggle_subscription_active_status_in_db(sub_id):
    try:
        with get_db_connection() as conn:
            current_status_row = conn.execute("SELECT is_active FROM subscriptions WHERE id = ?", (sub_id,)).fetchone()
            if current_status_row:
                new_status = not current_status_row['is_active']
                conn.execute("UPDATE subscriptions SET is_active = ? WHERE id = ?", (new_status, sub_id))
                conn.commit()
                return new_status
            else:
                logger.warning(f"尝试切换状态的订阅 {sub_id} 未找到。")
                return None
    except sqlite3.Error as e:
        logger.error(f"切换订阅 {sub_id} 状态时发生数据库错误: {e}")
        return None

def update_subscription_details_in_db(sub_id, name, url, interval_minutes, bark_key):
    try:
        with get_db_connection() as conn:
            conn.execute(
                """UPDATE subscriptions
                   SET name = ?, url = ?, interval_minutes = ?, bark_key = ?
                   WHERE id = ?""",
                (name, url, interval_minutes, bark_key, sub_id)
            )
            conn.commit()
            return True
    except sqlite3.IntegrityError:
        logger.error(f"更新订阅 {sub_id} 失败: URL '{url}' 可能已被其他订阅使用。")
        return False
    except sqlite3.Error as e:
        logger.error(f"更新订阅 {sub_id} 时发生数据库错误: {e}")
        return False

# --- 数据库操作函数 (总结相关) ---
def get_summary_config():
    try:
        with get_db_connection() as conn:
            config = conn.execute("SELECT * FROM summary_config WHERE id = 1").fetchone()
            return config
    except sqlite3.Error as e:
        logger.error(f"获取总结配置时发生数据库错误: {e}")
        return None

def update_summary_config(api_key, prompt, interval_hours, summary_bark_key):
    try:
        with get_db_connection() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO summary_config (id, interval_hours) 
                   VALUES (1, 24)"""
            )
            conn.execute(
                """UPDATE summary_config
                   SET gemini_api_key = ?,
                       summary_prompt = ?,
                       interval_hours = ?,
                       summary_bark_key = ?
                   WHERE id = 1""",
                (api_key, prompt, interval_hours, summary_bark_key)
            )
            conn.commit()
            logger.info(f"总结配置已更新: API Key (已设置: {'是' if api_key else '否'}), Interval: {interval_hours}h, Bark Key (已设置: {'是' if summary_bark_key else '否'})")
            return True
    except sqlite3.Error as e:
        logger.error(f"更新总结配置时发生数据库错误: {e}")
        return False

def save_summary_result(summary_text):
    try:
        with get_db_connection() as conn:
            conn.execute(
                "UPDATE summary_config SET last_summary = ?, last_summary_at = ? WHERE id = 1",
                (summary_text, datetime.now(timezone.utc))
            )
            conn.commit()
    except sqlite3.Error as e:
        logger.error(f"保存总结结果时发生数据库错误: {e}")

def get_daily_feed_titles():
    try:
        config_row = get_summary_config()
        interval_hours = 24
        if config_row and config_row['interval_hours'] is not None:
            interval_hours = config_row['interval_hours']
        
        with get_db_connection() as conn:
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=interval_hours)
            titles = conn.execute(
                """SELECT s.name, f.title, f.fetched_at
                   FROM feed_items f
                   JOIN subscriptions s ON f.subscription_id = s.id
                   WHERE f.fetched_at >= ?""",
                (cutoff_time,)
            ).fetchall()
            return titles
    except sqlite3.Error as e:
        logger.error(f"获取每日订阅标题时发生数据库错误: {e}")
        return []

# --- RSS Processing and Bark Notification ---
async def fetch_feed_content(url, sub_id):
    headers = {'User-Agent': f'RSS-to-Bark-Pusher/1.4 (sub_id:{sub_id})'}
    timeout_config = aiohttp.ClientTimeout(total=FEED_REQUEST_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout_config) as session:
        try:
            async with session.get(url, headers=headers) as response:
                response.raise_for_status()
                content = await response.read()
                logger.debug(f"成功获取订阅内容，字节数: {len(content)}")
                return content
        except asyncio.TimeoutError:
            logger.error(f"抓取订阅 {url} 超时 (超过 {FEED_REQUEST_TIMEOUT} 秒)。")
            return None
        except aiohttp.ClientResponseError as e:
            logger.error(f"抓取订阅 {url} 时发生 HTTP 错误: {e.status} {e.message}")
            return None
        except aiohttp.ClientError as e:
            logger.error(f"抓取订阅 {url} 失败: {e}")
            return None

def process_feed(subscription_id, is_test_run=False):
    sub = get_subscription_by_id(subscription_id)
    if not sub:
        logger.warning(f"订阅 {subscription_id} 在 process_feed 中未找到，跳过处理。")
        return
    
    logger.info(f"开始处理订阅: {sub['name']} ({sub['url']}) (Active: {sub['is_active']}, Test: {is_test_run})")
    update_subscription_last_checked(sub['id'])

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    feed_content = loop.run_until_complete(fetch_feed_content(sub['url'], sub['id']))
    loop.close()

    if feed_content is None:
        return

    feed_data = feedparser.parse(feed_content)
    if feed_data.bozo:
        bozo_exc_type = type(feed_data.bozo_exception).__name__
        bozo_exc_msg = str(feed_data.bozo_exception)
        logger.warning(f"订阅 {sub['name']} ({sub['url']}) 格式可能不正确: {bozo_exc_type} - {bozo_exc_msg}")
    
    if not feed_data.entries:
        logger.info(f"订阅 {sub['name']} ({sub['url']}) 没有条目。")
        return

    def get_entry_guid(entry):
        return entry.get('id', entry.get('link', entry.get('title', f"no_guid_fallback_{datetime.now(timezone.utc).timestamp()}")))

    try:
        with get_db_connection() as conn:
            for entry in feed_data.entries:
                title = entry.get('title', '无标题')
                guid = get_entry_guid(entry)
                conn.execute(
                    "INSERT OR IGNORE INTO feed_items (subscription_id, title, guid, fetched_at) VALUES (?, ?, ?, ?)",
                    (sub['id'], title, guid, datetime.now(timezone.utc))
                )
            conn.commit()
    except sqlite3.Error as e:
        logger.error(f"保存订阅 {sub['name']} 的条目到数据库时出错: {e}")

    last_known_guid = sub['last_fetched_item_guid']
    temp_new_items = []
    if not last_known_guid and not is_test_run:
        if feed_data.entries:
            temp_new_items.append(feed_data.entries[0])
            logger.info(f"首次运行 {sub['name']}, 标记最新条目: {feed_data.entries[0].get('title', '无标题')}")
    else:
        for entry in feed_data.entries:
            entry_guid = get_entry_guid(entry)
            if entry_guid == last_known_guid:
                break
            temp_new_items.append(entry)
    new_items_to_notify = list(reversed(temp_new_items))

    if not sub['is_active'] and not is_test_run:
        logger.info(f"订阅 {sub['name']} 未激活，开始检查关键词触发。")
        keyword_triggers = get_all_keyword_triggers()
        
        if new_items_to_notify and keyword_triggers:
            notified_guids_this_run = set()
            for item in new_items_to_notify:
                item_title = item.get('title', '无标题')
                item_guid = get_entry_guid(item)
                
                if item_guid in notified_guids_this_run:
                    continue

                for trigger in keyword_triggers:
                    keyword = trigger['keyword']
                    if keyword.lower() in item_title.lower():
                        logger.info(f"关键词 '{keyword}' 命中: 订阅='{sub['name']}', 标题='{item_title}'")
                        
                        link = item.get('link', '')
                        raw_summary = item.get('summary', item.get('description', item_title))
                        soup = BeautifulSoup(raw_summary, "html.parser")
                        body_content = soup.get_text(separator=' ', strip=True)
                        
                        notification_title = f"[关键词: {keyword}] {sub['name']}: {item_title}"
                        
                        send_bark_notification(
                            device_key=trigger['bark_key'],
                            title=notification_title,
                            body=body_content[:500],
                            url=link if link else None,
                            sound="glass",
                            group=f"关键词-{sub['name']}"
                        )
                        notified_guids_this_run.add(item_guid)
                        break 
        
        if feed_data.entries:
            latest_entry_guid = get_entry_guid(feed_data.entries[0])
            if sub['last_fetched_item_guid'] != latest_entry_guid:
                 update_subscription_last_item(sub['id'], latest_entry_guid)
                 logger.info(f"非激活订阅 {sub['name']} 更新了 last_fetched_item_guid 为: {latest_entry_guid}")
        return

    items_for_active_or_test = []
    if is_test_run:
        if feed_data.entries:
            items_for_active_or_test.append(feed_data.entries[0])
            logger.info(f"[测试模式] 为 {sub['name']} 准备推送最新条目: {feed_data.entries[0].get('title', '无标题')}")
        else:
            logger.info(f"[测试模式] 订阅 {sub['name']} 没有条目可供测试。")
            return
    else:
        items_for_active_or_test = new_items_to_notify

    if not items_for_active_or_test:
        logger.info(f"订阅 {sub['name']} 没有新内容。")
        if feed_data.entries and not is_test_run:
            latest_entry_guid = get_entry_guid(feed_data.entries[0])
            if sub['last_fetched_item_guid'] != latest_entry_guid:
                update_subscription_last_item(sub['id'], latest_entry_guid)
                logger.info(f"订阅 {sub['name']} 没有新通知内容，但更新了 last_fetched_item_guid 为: {latest_entry_guid}")
        return

    latest_sent_guid_this_run = None
    effective_title_prefix = "[测试] " if is_test_run else ""
    success_flag = False

    if len(items_for_active_or_test) == 1:
        item = items_for_active_or_test[0]
        title = item.get('title', '无标题')
        link = item.get('link', '')
        raw_summary = item.get('summary', item.get('description', title))
        soup = BeautifulSoup(raw_summary, "html.parser")
        body_content = soup.get_text(separator=' ', strip=True)
        effective_title = f"{effective_title_prefix}{sub['name']}: {title}"

        logger.info(f"准备发送 Bark 通知 (单个条目): Feed='{sub['name']}', Title='{title}' (Test: {is_test_run})")
        success_flag, response_data = send_bark_notification(
            device_key=sub['bark_key'],
            title=effective_title,
            body=body_content[:500],
            url=link if link else None,
            sound="glass",
            group=sub['name']
        )

        if success_flag:
            logger.info(f"Bark 通知发送成功 for '{title}'. Message ID: {response_data.get('messageid', 'N/A')}")
            if not is_test_run:
                latest_sent_guid_this_run = get_entry_guid(item)
        else:
            logger.error(f"Bark 通知发送失败 for '{title}'. 错误: {response_data.get('message', '未知错误')}")

    elif len(items_for_active_or_test) > 1:
        num_new_items = len(items_for_active_or_test)
        aggregated_title = f"{effective_title_prefix}{sub['name']} 有 {num_new_items} 条新更新"
        aggregated_body_titles_part = []
        aggregated_body_links_part = []

        for i, item in enumerate(items_for_active_or_test):
            item_title = item.get('title', '无标题')
            item_link = item.get('link', '')
            aggregated_body_titles_part.append(f"{i+1}. {item_title}")
            if item_link:
                aggregated_body_links_part.append(f"链接{i+1}: {item_link}")
            else:
                aggregated_body_links_part.append(f"链接{i+1}: (无链接)")

        aggregated_body = "\n".join(aggregated_body_titles_part)
        if aggregated_body_links_part:
            aggregated_body += "\n---\n"
            aggregated_body += "\n".join(aggregated_body_links_part)
        
        primary_url_for_notification = items_for_active_or_test[-1].get('link', sub['url'])

        logger.info(f"准备发送 Bark 通知 (聚合 {num_new_items} 条目): Feed='{sub['name']}' (Test: {is_test_run})")
        success_flag, response_data = send_bark_notification(
            device_key=sub['bark_key'],
            title=aggregated_title,
            body=aggregated_body[:2000],
            url=primary_url_for_notification,
            sound="glass",
            group=sub['name']
        )

        if success_flag:
            logger.info(f"聚合 Bark 通知发送成功 for {num_new_items} items from '{sub['name']}'. Message ID: {response_data.get('messageid', 'N/A')}")
            if not is_test_run:
                latest_item_in_batch = items_for_active_or_test[-1]
                latest_sent_guid_this_run = get_entry_guid(latest_item_in_batch)
        else:
            logger.error(f"聚合 Bark 通知发送失败 for '{sub['name']}'. 错误: {response_data.get('message', '未知错误')}")

    if latest_sent_guid_this_run and not is_test_run:
        update_subscription_last_item(sub['id'], latest_sent_guid_this_run)
        logger.info(f"更新订阅 {sub['name']} 的 last_fetched_item_guid 为: {latest_sent_guid_this_run}")
    elif is_test_run and items_for_active_or_test:
        logger.info(f"[测试模式] 订阅 {sub['name']} 的测试通知已尝试发送。last_fetched_item_guid 未更新。")

    del feed_data
    gc.collect()

# --- Gemini Summary Processing ---
def generate_daily_summary():
    config_row = get_summary_config()
    if not config_row or not config_row['gemini_api_key']:
        logger.warning("未配置 Gemini API Key，跳过每日总结。")
        return
    if not config_row['summary_bark_key']:
        logger.warning("未配置总结 Bark Key，跳过每日总结通知。")
        return
    if not genai or not types:
        logger.error("Gemini 库未加载，无法生成总结。")
        return

    titles = get_daily_feed_titles()
    interval_hours = config_row['interval_hours'] if config_row['interval_hours'] is not None else 24
    if not titles:
        logger.info(f"过去{interval_hours}小时内没有新订阅标题，跳过总结。")
        return

    sub_titles = {}
    for title_row_item in titles:
        sub_name = title_row_item['name']
        if sub_name not in sub_titles:
            sub_titles[sub_name] = []
        sub_titles[sub_name].append(title_row_item['title'])

    prompt_template = config_row['summary_prompt'] or "请用简洁的中文总结以下RSS订阅的标题内容，突出每组订阅的关键点，分组显示：\n\n{sub_titles}"
    
    formatted_titles_list = []
    for sub_name, sub_feed_titles in sub_titles.items():
        formatted_titles_list.append(f"{sub_name}: {', '.join(sub_feed_titles)}")
    formatted_titles_string = "\n".join(formatted_titles_list)
    
    final_prompt = prompt_template.replace("{sub_titles}", formatted_titles_string)

    try:
        client = genai.Client(api_key=config_row['gemini_api_key'])
        grounding_tool = types.Tool(google_search=types.GoogleSearch())
        config = types.GenerateContentConfig(tools=[grounding_tool])
        response = client.models.generate_content(
            model=os.environ.get('GEMINI_MODEL_NAME', 'gemini-2.5-flash'),
            contents=final_prompt,
            config=config,
        )
        summary_text = response.text
        logger.info("Gemini API 成功生成总结。")

        save_summary_result(summary_text)

        success, response_data = send_bark_notification(
            device_key=config_row['summary_bark_key'],
            title="每日RSS总结",
            body=summary_text[:2000],
            sound="glass",
            group="每日总结"
        )
        if success:
            logger.info(f"每日总结 Bark 通知发送成功。Message ID: {response_data.get('messageid', 'N/A')}")
        else:
            logger.error(f"每日总结 Bark 通知发送失败。错误: {response_data.get('message', '未知错误')}")
    except Exception as e:
        logger.error(f"调用 Gemini API 生成总结失败: {e}", exc_info=True)
    finally:
        gc.collect()

# --- Scheduler Management ---
def schedule_feed_job(subscription):
    job_id = f"feed_{subscription['id']}"
    try:
        existing_job = scheduler.get_job(job_id)
        if existing_job:
            scheduler.remove_job(job_id)
            logger.info(f"已移除现有任务: {job_id} (在重新调度前)")
    except JobLookupError:
        logger.debug(f"任务 {job_id} 未找到，无需移除。")
    except Exception as e:
        logger.error(f"移除任务 {job_id} 时发生错误: {e}", exc_info=True)

    try:
        scheduler.add_job(
            func=process_feed,
            trigger=IntervalTrigger(minutes=subscription['interval_minutes'], timezone=timezone.utc, jitter=30),
            args=[subscription['id']],
            id=job_id,
            name=f"Check {subscription['name']}",
            replace_existing=True,
            next_run_time=datetime.now(timezone.utc),
            misfire_grace_time=60
        )
        logger.info(f"已为 '{subscription['name']}' (ID: {subscription['id']}, Active: {subscription['is_active']}) 调度任务，间隔: {subscription['interval_minutes']} 分钟（带30秒jitter）。下次运行：ASAP")
    except Exception as e:
        logger.error(f"为 '{subscription['name']}' (ID: {subscription['id']}) 调度任务失败: {e}", exc_info=True)

def schedule_summary_job():
    config_row = get_summary_config()
    if not config_row or not config_row['interval_hours']:
        logger.info("未配置总结间隔或总结间隔为0，跳过总结任务调度。")
        return
    if config_row['interval_hours'] < 1:
        logger.warning(f"总结间隔配置为 {config_row['interval_hours']} 小时，至少应为1小时。跳过总结任务调度。")
        return

    job_id = "daily_summary"
    try:
        existing_job = scheduler.get_job(job_id)
        if existing_job:
            scheduler.remove_job(job_id)
            logger.info(f"已移除现有总结任务: {job_id}")
    except JobLookupError:
        logger.debug("总结任务未找到，无需移除。")
    try:
        scheduler.add_job(
            func=generate_daily_summary,
            trigger=IntervalTrigger(hours=config_row['interval_hours'], timezone=BEIJING_TZ, jitter=30),
            id=job_id,
            name="Daily Summary",
            replace_existing=True,
            next_run_time=datetime.now(timezone.utc) + timedelta(minutes=5)
        )
        logger.info(f"已调度每日总结任务，每 {config_row['interval_hours']} 小时运行一次（带30秒jitter），下次运行：ASAP (approx. 5 mins from now)")
    except Exception as e:
        logger.error(f"调度每日总结任务失败: {e}", exc_info=True)

def schedule_cleanup_job():
    job_id = "cleanup_feed_items"
    try:
        existing_job = scheduler.get_job(job_id)
        if existing_job:
            scheduler.remove_job(job_id)
            logger.info(f"已移除现有清理任务: {job_id}")
    except JobLookupError:
        logger.debug("清理任务未找到，无需移除。")
    try:
        scheduler.add_job(
            func=cleanup_old_feed_items,
            trigger=IntervalTrigger(days=1, timezone=timezone.utc, jitter=30),
            id=job_id,
            name="Cleanup Old Feed Items",
            replace_existing=True,
            next_run_time=datetime.now(timezone.utc) + timedelta(minutes=10)
        )
        logger.info("已调度每日清理任务，每24小时运行一次（带30秒jitter），下次运行：ASAP (approx. 10 mins from now)")
    except Exception as e:
        logger.error(f"调度每日清理任务失败: {e}", exc_info=True)

def reschedule_all_jobs():
    logger.info("重新加载并调度所有订阅任务和总结任务...")
    try:
        subscriptions = get_all_subscriptions()
    except Exception as e:
        logger.error(f"重新调度任务时无法获取订阅列表: {e}", exc_info=True)
        return

    for job in scheduler.get_jobs():
        if job.id.startswith("feed_") or job.id == "daily_summary" or job.id == "cleanup_feed_items":
            try:
                scheduler.remove_job(job.id)
                logger.info(f"已移除旧任务: {job.id} (在重新调度所有任务前)")
            except JobLookupError:
                pass
            except Exception as e:
                logger.error(f"移除旧任务 {job.id} 时发生错误: {e}", exc_info=True)

    for sub in subscriptions:
        schedule_feed_job(sub)

    schedule_summary_job()
    schedule_cleanup_job()
    logger.info("所有任务重新调度完成。")

# --- Flask Routes ---
@app.route('/', methods=['GET'])
def index():
    try:
        subscriptions_data = get_all_subscriptions()
    except Exception as e:
        logger.error(f"主页加载订阅时出错: {e}", exc_info=True)
        flash("加载订阅列表时出错，请查看日志。", "error")
        subscriptions_data = []
    return render_template('index.html', subscriptions=subscriptions_data)

@app.route('/add', methods=['POST'])
def add_subscription():
    name = request.form.get('name', '').strip()
    url = request.form.get('url', '').strip()
    interval_str = request.form.get('interval_minutes', '60').strip()
    bark_key = request.form.get('bark_key', '').strip()

    if not all([name, url, bark_key, interval_str]):
        flash('所有字段都是必填的。', 'error')
        return redirect(url_for('index'))
    
    try:
        interval_minutes = int(interval_str)
        if interval_minutes < 1:
            flash('抓取间隔不能小于1分钟。', 'error')
            return redirect(url_for('index'))
    except ValueError:
        flash('抓取间隔必须是有效的数字。', 'error')
        return redirect(url_for('index'))

    sub_id = add_sub_to_db(name, url, interval_minutes, bark_key)
    if sub_id:
        new_sub = get_subscription_by_id(sub_id)
        if new_sub:
            schedule_feed_job(new_sub)
            flash(f"订阅 '{name}' 添加成功并已调度。", 'success')
        else:
            flash(f"订阅 '{name}' 添加到数据库后无法立即检索。", 'error')
    else:
        flash(f"添加订阅 '{name}' 失败。可能URL已存在或数据库错误，请检查日志。", 'error')
        
    return redirect(url_for('index'))

@app.route('/edit/<int:sub_id>', methods=['GET'])
def edit_subscription_page(sub_id):
    sub = get_subscription_by_id(sub_id)
    if not sub:
        flash("未找到要编辑的订阅。", 'error')
        return redirect(url_for('index'))
    return render_template('edit_subscription.html', subscription=sub)

@app.route('/update/<int:sub_id>', methods=['POST'])
def update_subscription_action(sub_id):
    original_sub = get_subscription_by_id(sub_id)
    if not original_sub:
        flash("未找到要更新的订阅。", 'error')
        return redirect(url_for('index'))

    name = request.form.get('name', '').strip()
    url_new = request.form.get('url', '').strip()
    interval_minutes_str = request.form.get('interval_minutes', '').strip()
    bark_key = request.form.get('bark_key', '').strip()

    current_form_data = dict(original_sub)
    current_form_data.update({
        'name': name,
        'url': url_new,
        'interval_minutes': interval_minutes_str,
        'bark_key': bark_key
    })

    if not all([name, url_new, bark_key, interval_minutes_str]):
        flash('所有字段都是必填的。', 'error')
        return render_template('edit_subscription.html', subscription=current_form_data)
    
    try:
        interval_minutes = int(interval_minutes_str)
        if interval_minutes < 1:
            flash('抓取间隔不能小于1分钟。', 'error')
            current_form_data['interval_minutes'] = interval_minutes
            return render_template('edit_subscription.html', subscription=current_form_data)
    except ValueError:
        flash('抓取间隔必须是有效的数字。', 'error')
        return render_template('edit_subscription.html', subscription=current_form_data)
    
    current_form_data['interval_minutes'] = interval_minutes

    if url_new != original_sub['url']:
        try:
            with get_db_connection() as conn:
                existing_sub_with_url = conn.execute(
                    "SELECT id FROM subscriptions WHERE url = ? AND id != ?", (url_new, sub_id)
                ).fetchone()
                if existing_sub_with_url:
                    flash(f"URL '{url_new}' 已被ID为 {existing_sub_with_url['id']} 的其他订阅使用。", 'error')
                    return render_template('edit_subscription.html', subscription=current_form_data)
        except sqlite3.Error as e:
            logger.error(f"检查URL重复时发生数据库错误: {e}")
            flash("检查URL时发生数据库错误，请重试。", 'error')
            return render_template('edit_subscription.html', subscription=current_form_data)

    success_db_update = update_subscription_details_in_db(sub_id, name, url_new, interval_minutes, bark_key)
    
    if success_db_update:
        updated_sub = get_subscription_by_id(sub_id)
        if updated_sub:
            schedule_feed_job(updated_sub)
            flash(f"订阅 '{name}' 更新成功并已重新调度。", 'success')
        else:
            flash(f"订阅 '{name}' 更新后无法从数据库重新加载。", 'error')
    else:
        flash(f"更新订阅 '{name}' 失败。请检查日志或确保URL唯一。", 'error')
        return render_template('edit_subscription.html', subscription=current_form_data)
            
    return redirect(url_for('index'))

@app.route('/delete/<int:sub_id>')
def delete_subscription(sub_id):
    sub = get_subscription_by_id(sub_id)
    if sub:
        job_id = f"feed_{sub['id']}"
        try:
            scheduler.remove_job(job_id)
            logger.info(f"已移除任务: {job_id} (因删除订阅)")
        except JobLookupError:
            logger.info(f"任务 {job_id} 未找到，可能已被移除或未调度 (删除订阅时)。")
        except Exception as e:
            logger.error(f"移除任务 {job_id} 时发生错误: {e}", exc_info=True)

        delete_sub_from_db(sub_id)
        flash(f"订阅 '{sub['name']}' 已删除。", 'success')
    else:
        flash("未找到要删除的订阅。", 'error')
    return redirect(url_for('index'))

@app.route('/test/<int:sub_id>')
def test_subscription(sub_id):
    sub = get_subscription_by_id(sub_id)
    if sub:
        flash(f"正在测试订阅 '{sub['name']}'... 请检查你的 Bark 设备。如果源有最新内容，将会收到带'[测试]'前缀的通知。", 'info')
        try:
            process_feed(sub_id, is_test_run=True)
        except Exception as e:
            logger.error(f"测试订阅 {sub['name']} 时发生意外错误: {e}", exc_info=True)
            flash(f"测试订阅 '{sub['name']}' 时出错: {e}", 'error')
    else:
        flash("未找到要测试的订阅。", 'error')
    return redirect(url_for('index'))

@app.route('/toggle_status/<int:sub_id>')
def toggle_subscription_status(sub_id):
    sub_before_toggle = get_subscription_by_id(sub_id)
    if not sub_before_toggle:
        flash("无法更改订阅状态，订阅可能不存在。", 'error')
        return redirect(url_for('index'))

    new_status_bool = toggle_subscription_active_status_in_db(sub_id)
    if new_status_bool is not None:
        toggled_sub = get_subscription_by_id(sub_id)
        if toggled_sub:
            schedule_feed_job(toggled_sub)
            status_text = "激活" if new_status_bool else "暂停"
            flash(f"订阅 '{toggled_sub['name']}' 已设置为 {status_text} 状态并重新调度。", 'success')
        else:
            flash(f"订阅 '{sub_before_toggle['name']}' 状态已更改，但无法重新加载订阅信息。", 'error')
    else:
        flash(f"无法更改订阅 '{sub_before_toggle['name']}' 的状态。请检查日志。", 'error')
    return redirect(url_for('index'))

@app.route('/keywords', methods=['GET', 'POST'])
def keywords():
    if request.method == 'POST':
        keyword = request.form.get('keyword', '').strip()
        bark_key = request.form.get('bark_key', '').strip()

        if not keyword or not bark_key:
            flash('关键词和 Bark Key 不能为空。', 'error')
        else:
            if add_keyword_trigger(keyword, bark_key):
                flash(f"关键词 '{keyword}' 添加成功。", 'success')
            else:
                flash(f"添加关键词 '{keyword}' 失败，可能已存在。", 'error')
        return redirect(url_for('keywords'))

    triggers = get_all_keyword_triggers()
    return render_template('keywords.html', triggers=triggers)

@app.route('/keywords/delete/<int:keyword_id>')
def delete_keyword(keyword_id):
    if delete_keyword_trigger(keyword_id):
        flash('关键词已删除。', 'success')
    else:
        flash('删除关键词失败，请检查日志。', 'error')
    return redirect(url_for('keywords'))

@app.route('/summary_config', methods=['GET', 'POST'])
def summary_config():
    db_config_row = get_summary_config()
    
    if db_config_row:
        current_config_dict = dict(db_config_row)
    else:
        current_config_dict = {
            'id': 1, 'gemini_api_key': None, 'summary_prompt': None, 
            'interval_hours': 24, 'summary_bark_key': None, 
            'last_summary': None, 'last_summary_at': None
        }

    if request.method == 'POST':
        db_gemini_key = current_config_dict.get('gemini_api_key', '')
        db_summary_bark_key = current_config_dict.get('summary_bark_key', '')

        form_gemini_api_key_input = request.form.get('gemini_api_key', '').strip()
        form_summary_bark_key_input = request.form.get('summary_bark_key', '').strip()
        
        form_gemini_api_key_hidden = request.form.get('gemini_api_key_hidden', '').strip()
        form_summary_bark_key_hidden = request.form.get('summary_bark_key_hidden', '').strip()

        if form_gemini_api_key_input == '********':
            actual_gemini_api_key = form_gemini_api_key_hidden 
        else:
            actual_gemini_api_key = form_gemini_api_key_input

        if form_summary_bark_key_input == '********':
            actual_summary_bark_key = form_summary_bark_key_hidden
        else:
            actual_summary_bark_key = form_summary_bark_key_input
        
        summary_prompt_from_form = request.form.get('summary_prompt', '').strip()
        interval_hours_str_from_form = request.form.get('interval_hours', '24').strip()

        try:
            interval_hours_val = int(interval_hours_str_from_form)
            if interval_hours_val < 1:
                flash('总结间隔不能小于1小时。', 'error')
                form_data_for_render = current_config_dict.copy()
                form_data_for_render.update({
                    'gemini_api_key': actual_gemini_api_key, 
                    'summary_prompt': summary_prompt_from_form,
                    'interval_hours': interval_hours_str_from_form,
                    'summary_bark_key': actual_summary_bark_key
                })
                return render_template('summary_config.html', config=form_data_for_render, show_items_area=False, show_summary_area=False)
        except ValueError:
            flash('总结间隔必须是有效的数字。', 'error')
            form_data_for_render = current_config_dict.copy()
            form_data_for_render.update({
                'gemini_api_key': actual_gemini_api_key,
                'summary_prompt': summary_prompt_from_form,
                'interval_hours': interval_hours_str_from_form,
                'summary_bark_key': actual_summary_bark_key
            })
            return render_template('summary_config.html', config=form_data_for_render, show_items_area=False, show_summary_area=False)

        if update_summary_config(actual_gemini_api_key, summary_prompt_from_form, interval_hours_val, actual_summary_bark_key):
            schedule_summary_job()
            flash('总结配置已更新并重新调度。', 'success')
        else:
            flash('更新总结配置失败，请检查日志。', 'error')
        return redirect(url_for('summary_config'))

    feed_items_to_display = None 
    show_items_area_flag = False
    show_summary_area_flag = False

    if request.args.get('show_items', 'false').lower() == 'true':
        show_items_area_flag = True
        interval_hours_for_display = current_config_dict.get('interval_hours', 24)
        if interval_hours_for_display is None:
            interval_hours_for_display = 24
            
        feed_items_to_display = get_detailed_feed_items_for_summary(interval_hours_for_display)
        if not feed_items_to_display:
            flash(f"过去 {interval_hours_for_display} 小时内没有获取到任何 RSS 条目。", "info")
    
    if request.args.get('show_summary', 'false').lower() == 'true':
        show_summary_area_flag = True
        if not current_config_dict.get('last_summary'):
            flash("数据库中还没有保存任何总结。", "info")
            
    return render_template('summary_config.html', 
                           config=current_config_dict, 
                           feed_items_for_summary=feed_items_to_display, 
                           show_items_area=show_items_area_flag,
                           show_summary_area=show_summary_area_flag)

@app.route('/test_summary')
def test_summary():
    config_row = get_summary_config()
    if not config_row or not config_row['gemini_api_key']:
        flash('未配置 Gemini API Key，无法测试总结。', 'error')
        return redirect(url_for('summary_config'))
    if not config_row['summary_bark_key']:
        flash('未配置总结 Bark Key，无法测试总结通知。', 'error')
        return redirect(url_for('summary_config'))
    if not genai or not types:
        flash("Gemini 库未加载，无法测试总结。", "error")
        return redirect(url_for('summary_config'))

    titles = get_daily_feed_titles()
    interval_hours = config_row['interval_hours'] if config_row['interval_hours'] is not None else 24
    if not titles:
        flash(f'过去{interval_hours}小时内没有新订阅标题，无法生成测试总结。', 'info')
        return redirect(url_for('summary_config'))

    sub_titles = {}
    for title_row_item in titles:
        sub_name = title_row_item['name']
        if sub_name not in sub_titles:
            sub_titles[sub_name] = []
        sub_titles[sub_name].append(title_row_item['title'])

    prompt_template = config_row['summary_prompt'] or "请用简洁的中文总结以下RSS订阅的标题内容，突出每组订阅的关键点，分组显示：\n\n{sub_titles}"
    
    formatted_titles_list = []
    for sub, sub_feed_titles in sub_titles.items():
        formatted_titles_list.append(f"{sub}: {', '.join(sub_feed_titles)}")
    formatted_titles_string = "\n".join(formatted_titles_list)
    
    final_prompt = prompt_template.replace("{sub_titles}", formatted_titles_string)
    logger.info(f"测试总结使用的最终提示词: {final_prompt[:500]}...")

    try:
        client = genai.Client(api_key=config_row['gemini_api_key'])
        grounding_tool = types.Tool(google_search=types.GoogleSearch())
        config = types.GenerateContentConfig(tools=[grounding_tool])
        response = client.models.generate_content(
            model=os.environ.get('GEMINI_MODEL_NAME', 'gemini-2.5-flash'),
            contents=final_prompt,
            config=config,
        )
        summary_text = response.text
        logger.info("Gemini API 成功生成测试总结。")
        
        save_summary_result(summary_text)

        success, response_data = send_bark_notification(
            device_key=config_row['summary_bark_key'],
            title="[测试] 每日RSS总结",
            body=summary_text[:2000],
            sound="glass",
            group="每日总结"
        )
        if success:
            logger.info(f"测试总结 Bark 通知发送成功。Message ID: {response_data.get('messageid', 'N/A')}")
            flash('测试总结已生成并发送，请检查Bark设备。总结结果已更新到页面。', 'success')
            return redirect(url_for('summary_config', show_summary='true')) 
        else:
            logger.error(f"测试总结 Bark 通知发送失败。错误: {response_data.get('message', '未知错误')}")
            flash('测试总结通知发送失败，请检查日志。总结结果仍会更新到页面。', 'warning')
            return redirect(url_for('summary_config', show_summary='true'))
    except Exception as e:
        logger.error(f"测试总结失败: {e}", exc_info=True)
        flash(f"测试总结失败: {e}", 'error')

    return redirect(url_for('summary_config'))

# --- 应用初始化和启动 ---
db_dir_path = os.path.dirname(DATABASE_FILE)
if not os.path.exists(db_dir_path):
    try:
        os.makedirs(db_dir_path)
        logger.info(f"数据目录 {db_dir_path} 已创建 (在 app.py 启动时)。")
    except OSError as e:
        logger.critical(f"无法创建数据目录 {db_dir_path}: {e}。应用可能无法正常工作。", exc_info=True)

if not os.path.exists(DATABASE_FILE):
    logger.info(f"数据库文件 {DATABASE_FILE} 未找到，正在调用 init_db()...")
    try:
        init_db()
    except Exception as e:
        logger.critical(f"首次初始化数据库失败: {e}. 应用可能无法启动。", exc_info=True)
else:
    logger.info(f"使用现有数据库 {DATABASE_FILE}。调用 init_db() 以确保表结构和WAL模式...")
    try:
        init_db()
    except Exception as e:
        logger.warning(f"检查/更新现有数据库时出错: {e}. 应用将尝试继续。", exc_info=True)

if not scheduler.running:
    try:
        scheduler.start(paused=False)
        logger.info("调度器已启动。")
        reschedule_all_jobs()
    except Exception as e:
        logger.critical(f"调度器启动失败: {e}", exc_info=True)
else:
    logger.warning("调度器已在运行，可能由Gunicorn --reload或多次导入触发。跳过重复启动。")

if __name__ == '__main__':
    logger.info("以开发模式启动 Flask 应用 (python app.py)...")
    
    flask_debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    use_reloader_val = os.environ.get('FLASK_USE_RELOADER', 'False').lower() == 'true'

    for flask_handler in app.logger.handlers:
        flask_handler.setFormatter(beijing_formatter)
    
    werkzeug_logger = logging.getLogger('werkzeug')
    for werkzeug_handler in werkzeug_logger.handlers[:]:
        werkzeug_logger.removeHandler(werkzeug_handler)
    
    werkzeug_console_handler = logging.StreamHandler()
    werkzeug_console_handler.setFormatter(beijing_formatter)
    werkzeug_logger.addHandler(werkzeug_console_handler)
    werkzeug_logger.propagate = False

    aps_logger = logging.getLogger('apscheduler')
    for aps_handler in aps_logger.handlers[:]:
        aps_logger.removeHandler(aps_handler)
    aps_console_handler = logging.StreamHandler()
    aps_console_handler.setFormatter(beijing_formatter)
    aps_logger.addHandler(aps_console_handler)
    aps_logger.setLevel(os.environ.get('APS_LOG_LEVEL', 'WARNING').upper())

    app.run(host='0.0.0.0', port=5000, debug=flask_debug_mode, use_reloader=use_reloader_val)

    logger.info("开发服务器正在关闭...")
    if scheduler.running:
        try:
            logger.info("正在关闭调度器...")
            scheduler.shutdown()
            logger.info("调度器已关闭。")
        except Exception as e:
            logger.error(f"关闭调度器时出错: {e}", exc_info=True)