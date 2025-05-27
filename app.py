# app.py
import os
import sqlite3 # 仍然需要导入 sqlite3 来捕获特定的异常
import logging
from datetime import datetime, timezone, timedelta # timedelta 导入

import feedparser
import requests # for feedparser, and bark_sender might use it
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

# 从你的 database.py 模块导入
from database import get_db_connection, init_db, DATABASE_FILE, _db_lock

APP_SECRET_KEY = os.environ.get('APP_SECRET_KEY', os.urandom(24)) # 允许通过环境变量配置
FEED_REQUEST_TIMEOUT = int(os.environ.get('FEED_REQUEST_TIMEOUT', 20)) # 从环境变量读取超时时间

# --- 北京时区定义 ---
BEIJING_TZ = timezone(timedelta(hours=8), 'Asia/Shanghai')

# --- 日志配置 (使用北京时间) ---
LOG_LEVEL_STR = os.environ.get('LOG_LEVEL', 'INFO').upper()
LOG_LEVEL = getattr(logging, LOG_LEVEL_STR, logging.INFO)

class BeijingTimeFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        # 将 record.created (UTC timestamp) 转换为北京时间 datetime 对象
        dt_beijing = datetime.fromtimestamp(record.created, BEIJING_TZ)
        if datefmt:
            s = dt_beijing.strftime(datefmt)
        else:
            # 默认格式 (类似ISO，但带北京时区信息)
            s = dt_beijing.strftime('%Y-%m-%d %H:%M:%S')
            s += f',{int(record.msecs):03d} CST' # 添加毫秒和时区标识
        return s

# 配置日志基础设置
logger_format_string = '%(asctime)s - %(levelname)s - %(name)s - %(message)s'
# datefmt 将被 BeijingTimeFormatter 的 formatTime 方法使用
logger_date_format_string = '%Y-%m-%d %H:%M:%S' 

# 创建自定义的 Formatter 实例
beijing_formatter = BeijingTimeFormatter(fmt=logger_format_string, datefmt=logger_date_format_string)

# 获取 root logger
root_logger = logging.getLogger()
root_logger.setLevel(LOG_LEVEL) # 设置 root logger 的级别

# 清理 root logger 可能已有的 handlers (防止重复日志或旧格式)
for handler in root_logger.handlers[:]:
    root_logger.removeHandler(handler)

# 添加一个新的 StreamHandler，使用我们的北京时间 Formatter
console_handler = logging.StreamHandler()
console_handler.setFormatter(beijing_formatter)
root_logger.addHandler(console_handler)

logger = logging.getLogger(__name__) # 获取当前模块的 logger，它将继承 root logger 的配置

app = Flask(__name__)
app.secret_key = APP_SECRET_KEY

# --- Jinja2 过滤器，用于在模板中将UTC时间转换为北京时间并格式化 ---
def format_datetime_to_beijing_time(dt_obj, fmt='%Y-%m-%d %H:%M:%S'):
    if dt_obj is None:
        return '从未' # 或者其他你希望的占位符
    if not isinstance(dt_obj, datetime):
        return str(dt_obj) # 如果不是 datetime 对象，直接返回字符串

    # 确保 dt_obj 是时区感知的UTC时间
    if dt_obj.tzinfo is None:
        dt_obj = dt_obj.replace(tzinfo=timezone.utc) # 假设 naive datetime 是 UTC
    
    beijing_dt = dt_obj.astimezone(BEIJING_TZ)
    return beijing_dt.strftime(fmt)

app.jinja_env.filters['beijing_time'] = format_datetime_to_beijing_time


scheduler = BackgroundScheduler(timezone=timezone.utc) # 调度器内部仍使用UTC

# --- 数据库操作函数 ---
# (所有数据库时间戳存储 datetime.now(timezone.utc)，保持不变)
def add_sub_to_db(name, url, interval_minutes, bark_key):
    try:
        with get_db_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO subscriptions (name, url, interval_minutes, bark_key, is_active, created_at, last_checked_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, url, interval_minutes, bark_key, True, datetime.now(timezone.utc), None) # created_at 使用 UTC
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
                (item_guid, datetime.now(timezone.utc), sub_id) # last_checked_at 使用 UTC
            )
            conn.commit()
    except sqlite3.Error as e:
        logger.error(f"更新订阅 {sub_id} 的 last_item 时发生数据库错误: {e}")

def update_subscription_last_checked(sub_id):
    try:
        with get_db_connection() as conn:
            conn.execute(
                "UPDATE subscriptions SET last_checked_at = ? WHERE id = ?",
                (datetime.now(timezone.utc), sub_id) # last_checked_at 使用 UTC
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

# --- RSS Processing and Bark Notification ---
def process_feed(subscription_id, is_test_run=False):
    sub = get_subscription_by_id(subscription_id)
    if not sub:
        logger.warning(f"订阅 {subscription_id} 在 process_feed 中未找到，跳过处理。")
        return
    
    if not sub['is_active'] and not is_test_run:
        logger.info(f"订阅 {subscription_id} ({sub['name']}) 未激活，跳过处理。")
        update_subscription_last_checked(sub['id']) 
        return

    logger.info(f"开始处理订阅: {sub['name']} ({sub['url']})")
    update_subscription_last_checked(sub['id'])

    feed_content = None
    try:
        headers = {'User-Agent': f'RSS-to-Bark-Pusher/1.4 (sub_id:{sub["id"]})'} 
        response = requests.get(sub['url'], headers=headers, timeout=FEED_REQUEST_TIMEOUT)
        response.raise_for_status()
        feed_content = response.content
        logger.debug(f"成功获取订阅 {sub['name']} 内容，字节数: {len(feed_content)}")
    except requests.exceptions.Timeout:
        logger.error(f"抓取订阅 {sub['name']} ({sub['url']}) 超时 (超过 {FEED_REQUEST_TIMEOUT} 秒)。")
        return
    except requests.exceptions.HTTPError as e:
        logger.error(f"抓取订阅 {sub['name']} ({sub['url']}) 时发生 HTTP 错误: {e.response.status_code} {e.response.reason}")
        return
    except requests.exceptions.RequestException as e:
        logger.error(f"抓取订阅 {sub['name']} ({sub['url']}) 失败: {e}")
        return
    except Exception as e:
        logger.error(f"获取订阅 {sub['name']} ({sub['url']}) 内容时发生未知错误: {e}", exc_info=True)
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

    last_known_guid = sub['last_fetched_item_guid']
    new_items_to_notify = []

    if is_test_run:
        if feed_data.entries:
            new_items_to_notify.append(feed_data.entries[0]) 
            logger.info(f"[测试模式] 为 {sub['name']} 准备推送最新条目: {feed_data.entries[0].get('title', '无标题')}")
        else:
            logger.info(f"[测试模式] 订阅 {sub['name']} 没有条目可供测试。")
            return 
    else:
        temp_new_items = []
        if not last_known_guid: 
            if feed_data.entries:
                # For first run, only take the newest single item to avoid flood.
                # User can manually mark all as read if they want older items.
                temp_new_items.append(feed_data.entries[0])
                logger.info(f"首次运行 {sub['name']}, 标记最新条目: {feed_data.entries[0].get('title', '无标题')}")
        else:
            for entry in feed_data.entries: 
                entry_guid = get_entry_guid(entry)
                if entry_guid == last_known_guid:
                    break 
                temp_new_items.append(entry) 
        new_items_to_notify = list(reversed(temp_new_items)) # Oldest new to newest new

    if not new_items_to_notify:
        logger.info(f"订阅 {sub['name']} 没有新内容。")
        return

    latest_sent_guid_this_run = None
    effective_title_prefix = "[测试] " if is_test_run else ""
    success_flag = False # Flag to track if notification was sent successfully

    if len(new_items_to_notify) == 1:
        item = new_items_to_notify[0]
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
            body=body_content[:500], # Truncate body for single item
            url=link if link else None, 
            group=sub['name']
        )

        if success_flag:
            logger.info(f"Bark 通知发送成功 for '{title}'. Message ID: {response_data.get('messageid', 'N/A')}")
            if not is_test_run:
                latest_sent_guid_this_run = get_entry_guid(item)
        else:
            logger.error(f"Bark 通知发送失败 for '{title}'. 错误: {response_data.get('message', '未知错误')}")
            
    elif len(new_items_to_notify) > 1:
        num_new_items = len(new_items_to_notify)
        aggregated_title = f"{effective_title_prefix}{sub['name']} 有 {num_new_items} 条新更新"
        
        aggregated_body_titles_part = []
        aggregated_body_links_part = []
        
        # new_items_to_notify is already chronologically ordered (oldest new to newest new)
        for i, item in enumerate(new_items_to_notify):
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
        
        primary_url_for_notification = new_items_to_notify[-1].get('link', sub['url'])

        logger.info(f"准备发送 Bark 通知 (聚合 {num_new_items} 条目): Feed='{sub['name']}' (Test: {is_test_run})")
        success_flag, response_data = send_bark_notification(
            device_key=sub['bark_key'],
            title=aggregated_title,
            body=aggregated_body[:2000], # Increased limit for aggregated body
            url=primary_url_for_notification, 
            group=sub['name']
        )

        if success_flag:
            logger.info(f"聚合 Bark 通知发送成功 for {num_new_items} items from '{sub['name']}'. Message ID: {response_data.get('messageid', 'N/A')}")
            if not is_test_run:
                latest_item_in_batch = new_items_to_notify[-1] 
                latest_sent_guid_this_run = get_entry_guid(latest_item_in_batch)
        else:
            logger.error(f"聚合 Bark 通知发送失败 for '{sub['name']}'. 错误: {response_data.get('message', '未知错误')}")

    if latest_sent_guid_this_run and not is_test_run: 
        update_subscription_last_item(sub['id'], latest_sent_guid_this_run)
        logger.info(f"更新订阅 {sub['name']} 的 last_fetched_item_guid 为: {latest_sent_guid_this_run}")
    elif is_test_run and new_items_to_notify: 
        logger.info(f"[测试模式] 订阅 {sub['name']} 的测试通知已尝试发送。last_fetched_item_guid 未更新。")


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


    if subscription['is_active']:
        try:
            scheduler.add_job(
                func=process_feed,
                trigger=IntervalTrigger(minutes=subscription['interval_minutes'], timezone=timezone.utc), 
                args=[subscription['id']],
                id=job_id,
                name=f"Check {subscription['name']}",
                replace_existing=True, 
                next_run_time=datetime.now(timezone.utc) 
            )
            logger.info(f"已为 '{subscription['name']}' (ID: {subscription['id']}) 调度任务，间隔: {subscription['interval_minutes']} 分钟。下次运行：ASAP")
        except Exception as e:
            logger.error(f"为 '{subscription['name']}' (ID: {subscription['id']}) 调度任务失败: {e}", exc_info=True)
    else:
        logger.info(f"订阅 '{subscription['name']}' (ID: {subscription['id']}) 未激活，不调度任务。")

def reschedule_all_jobs():
    logger.info("重新加载并调度所有激活的订阅任务...")
    try:
        subscriptions = get_all_subscriptions()
    except Exception as e:
        logger.error(f"重新调度任务时无法获取订阅列表: {e}", exc_info=True)
        return 

    for job in scheduler.get_jobs():
        if job.id.startswith("feed_"):
            try:
                scheduler.remove_job(job.id)
                logger.info(f"已移除旧任务: {job.id} (在重新调度所有任务前)")
            except JobLookupError:
                pass 
            except Exception as e:
                logger.error(f"移除旧任务 {job.id} 时发生错误: {e}", exc_info=True)


    for sub in subscriptions:
        if sub['is_active']:
            schedule_feed_job(sub)
        
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

    current_form_data = dict(original_sub) # Start with original data
    # Update with new form data, converting to string where necessary for re-rendering
    current_form_data.update({
        'name': name, 
        'url': url_new, 
        'interval_minutes': interval_minutes_str, # Keep as string for re-render
        'bark_key': bark_key
    })


    if not all([name, url_new, bark_key, interval_minutes_str]):
        flash('所有字段都是必填的。', 'error')
        # Pass current_form_data (which now includes user's attempted changes)
        return render_template('edit_subscription.html', subscription=current_form_data) 
    
    try:
        interval_minutes = int(interval_minutes_str)
        if interval_minutes < 1:
            flash('抓取间隔不能小于1分钟。', 'error')
            # Update current_form_data's interval_minutes to the parsed int for consistency if needed,
            # or just keep it as the string. For rendering, string is fine.
            # current_form_data['interval_minutes'] = interval_minutes # This would be int
            return render_template('edit_subscription.html', subscription=current_form_data)
    except ValueError:
        flash('抓取间隔必须是有效的数字。', 'error')
        return render_template('edit_subscription.html', subscription=current_form_data)

    # Check for URL uniqueness only if URL has changed
    if url_new != original_sub['url']:
        try:
            with get_db_connection() as conn:
                existing_sub_with_url = conn.execute(
                    "SELECT id FROM subscriptions WHERE url = ? AND id != ?", (url_new, sub_id)
                ).fetchone()
                if existing_sub_with_url:
                    flash(f"URL '{url_new}' 已被ID为 {existing_sub_with_url['id']} 的其他订阅使用。", 'error')
                    # Update current_form_data with the validated int interval before re-rendering
                    current_form_data['interval_minutes'] = interval_minutes 
                    return render_template('edit_subscription.html', subscription=current_form_data)
        except sqlite3.Error as e:
            logger.error(f"检查URL重复时发生数据库错误: {e}")
            flash("检查URL时发生数据库错误，请重试。", 'error')
            current_form_data['interval_minutes'] = interval_minutes
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
        current_form_data['interval_minutes'] = interval_minutes # Use the validated int
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
            flash(f"订阅 '{toggled_sub['name']}' 已设置为 {status_text} 状态。", 'success')
        else:
            flash(f"订阅 '{sub_before_toggle['name']}' 状态已更改，但无法重新加载订阅信息。", 'error')
    else:
        flash(f"无法更改订阅 '{sub_before_toggle['name']}' 的状态。请检查日志。", 'error')
    return redirect(url_for('index'))


# --- 应用初始化和启动 ---
db_dir_path = os.path.dirname(DATABASE_FILE)
if not os.path.exists(db_dir_path):
    try:
        os.makedirs(db_dir_path)
        logger.info(f"数据目录 {db_dir_path} 已创建 (在 app.py 启动时)。")
    except OSError as e:
        logger.critical(f"无法创建数据目录 {db_dir_path}: {e}。应用可能无法正常工作。", exc_info=True)
        # Consider exiting if data directory cannot be created, as DB operations will fail
        
if not os.path.exists(DATABASE_FILE):
    logger.info(f"数据库文件 {DATABASE_FILE} 未找到，正在调用 init_db()...")
    try:
        init_db() 
    except Exception as e:
        logger.critical(f"首次初始化数据库失败: {e}. 应用可能无法启动。", exc_info=True)
        # Consider exiting
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
    logger.info("调度器已在运行 (可能由Gunicorn --reload或多次导入触发)。")


if __name__ == '__main__':
    logger.info("以开发模式启动 Flask 应用 (python app.py)...")
    
    flask_debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    use_reloader_val = os.environ.get('FLASK_USE_RELOADER', 'False').lower() == 'true' # Default to False if not set

    # 确保 Flask app logger 和 Werkzeug logger 也使用北京时间 formatter
    # 这在开发模式下尤其重要
    for flask_handler in app.logger.handlers:
        flask_handler.setFormatter(beijing_formatter)
    
    werkzeug_logger = logging.getLogger('werkzeug')
    # Clear existing werkzeug handlers to avoid duplicate logs if reloader is on
    for werkzeug_handler in werkzeug_logger.handlers[:]:
        werkzeug_logger.removeHandler(werkzeug_handler)
    
    # Add our custom formatted handler to werkzeug
    werkzeug_console_handler = logging.StreamHandler()
    werkzeug_console_handler.setFormatter(beijing_formatter)
    werkzeug_logger.addHandler(werkzeug_console_handler)
    werkzeug_logger.propagate = False # Prevent werkzeug logs from going to root logger if we added our own

    # APScheduler logger also needs formatting if its level is high enough to output
    aps_logger = logging.getLogger('apscheduler')
    for aps_handler in aps_logger.handlers[:]: # Clear existing if any
        aps_logger.removeHandler(aps_handler)
    aps_console_handler = logging.StreamHandler()
    aps_console_handler.setFormatter(beijing_formatter)
    aps_logger.addHandler(aps_console_handler)
    # Set APScheduler log level (e.g., WARNING to reduce noise, or inherit from root)
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