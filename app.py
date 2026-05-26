# app.py
import os
import sqlite3
import logging
import gc
import json
import secrets
from urllib.parse import urlparse
from datetime import datetime, timezone, timedelta
import feedparser
import aiohttp
import asyncio
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.jobstores.base import JobLookupError
from bs4 import BeautifulSoup

try:
    import paho.mqtt.publish as mqtt_publish
except ImportError:
    print("错误: 无法导入 paho.mqtt.publish。请确保已安装：pip install paho-mqtt")
    mqtt_publish = None

try:
    from bark_sender import send_bark_notification
except ImportError:
    print("错误: 无法导入 bark_sender.py。请确保该文件存在于同一目录下。")
    def send_bark_notification(device_key, body, title="", markdown="", **kwargs):
        print(f"[DUMMY BARK] To: {device_key}, Title: {title}, Body: {body}, Markdown: {markdown}")
        print(f"Other args: {kwargs}")
        return True, {"messageid": "dummy_id", "code": 200, "message": "Dummy success"}

try:
    from google import genai
    from google.genai import types
    from google.genai.errors import ClientError
except ImportError:
    print("错误: 无法导入 google.genai。请确保已安装：pip install -q -U google-genai ")
    genai = None
    types = None
    class ClientError(Exception):
        pass

try:
    from openai import OpenAI
except ImportError:
    print("错误: 无法导入 openai。请确保已安装：pip install openai")
    OpenAI = None

from database import (
    get_db_connection, init_db, cleanup_old_feed_items, DATABASE_FILE, _db_lock,
    get_detailed_feed_items_for_summary,
    get_all_keyword_triggers, add_keyword_trigger, delete_keyword_trigger,
    get_mqtt_config, save_mqtt_config
)

APP_SECRET_KEY = os.environ.get('APP_SECRET_KEY', os.urandom(24))
FEED_REQUEST_TIMEOUT = int(os.environ.get('FEED_REQUEST_TIMEOUT', 20))
FEED_FAILURE_WARNING_THRESHOLD = int(os.environ.get('FEED_FAILURE_WARNING_THRESHOLD', 3))
FEED_FAILURE_REMINDER_HOURS = int(os.environ.get('FEED_FAILURE_REMINDER_HOURS', 6))

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
ALLOWED_BARK_LEVELS = {'', 'timeSensitive'}

def normalize_bark_level(level):
    level_value = (level or '').strip()
    return level_value if level_value in ALLOWED_BARK_LEVELS else ''

def add_sub_to_db(name, url, interval_minutes, bark_key, bark_level=''):
    try:
        normalized_bark_level = normalize_bark_level(bark_level)
        with get_db_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO subscriptions (name, url, interval_minutes, bark_key, bark_level, is_active, created_at, last_checked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (name, url, interval_minutes, bark_key, normalized_bark_level, True, datetime.now(timezone.utc), None)
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

def update_subscription_last_item_link(sub_id, item_link):
    try:
        with get_db_connection() as conn:
            conn.execute(
                "UPDATE subscriptions SET last_fetched_item_link = ?, last_checked_at = ? WHERE id = ?",
                (item_link, datetime.now(timezone.utc), sub_id)
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

def update_subscription_details_in_db(sub_id, name, url, interval_minutes, bark_key, bark_level=''):
    try:
        normalized_bark_level = normalize_bark_level(bark_level)
        with get_db_connection() as conn:
            conn.execute(
                """UPDATE subscriptions
                   SET name = ?, url = ?, interval_minutes = ?, bark_key = ?, bark_level = ?
                   WHERE id = ?""",
                (name, url, interval_minutes, bark_key, normalized_bark_level, sub_id)
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

def normalize_ai_provider(provider):
    provider_value = (provider or 'gemini').strip().lower()
    return provider_value if provider_value in ('gemini', 'openai') else 'gemini'

def get_provider_display_name(provider):
    return 'OpenAI' if normalize_ai_provider(provider) == 'openai' else 'Gemini'

def get_summary_model_name(config_row, provider):
    if normalize_ai_provider(provider) == 'openai':
        return config_row['openai_model'] or os.environ.get('OPENAI_MODEL_NAME', 'gpt-4o-mini')
    return config_row['gemini_model'] or os.environ.get('GEMINI_MODEL_NAME', 'gemini-2.5-flash')

def get_summary_api_key(config_row, provider):
    if normalize_ai_provider(provider) == 'openai':
        return config_row['openai_api_key']
    return config_row['gemini_api_key']

def get_openai_base_url(config_row):
    return (config_row['openai_base_url'] or '').strip()

def base_url_host_matches(base_url, expected_host):
    if not base_url:
        return False
    parsed = urlparse(base_url if '://' in base_url else f"https://{base_url}")
    host = (parsed.hostname or '').lower()
    expected = expected_host.lower()
    return host == expected or host.endswith(f".{expected}")

def build_openai_client_kwargs(api_key, base_url=''):
    client_kwargs = {'api_key': api_key}
    normalized_base_url = (base_url or '').strip()
    if normalized_base_url:
        client_kwargs['base_url'] = normalized_base_url
    if base_url_host_matches(normalized_base_url, 'manyrouter.chaosyn.com'):
        client_kwargs['default_headers'] = {
            'User-Agent': 'RSS-Bark-Pusher/1.5',
            'X-Title': 'RSS Bark Pusher',
        }
    return client_kwargs

def get_summary_config_csrf_token():
    token = session.get('summary_config_csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['summary_config_csrf_token'] = token
    return token

def validate_summary_config_csrf(token):
    return bool(token) and secrets.compare_digest(token, session.get('summary_config_csrf_token', ''))

def resolve_masked_secret(input_value, hidden_value=None, stored_value=''):
    if input_value == '********':
        return stored_value or ''
    return input_value

def build_summary_prompt(titles, prompt_template):
    sub_titles = {}
    for title_row_item in titles:
        sub_name = title_row_item['name']
        if sub_name not in sub_titles:
            sub_titles[sub_name] = []
        sub_titles[sub_name].append(title_row_item['title'])

    formatted_titles_list = []
    for sub_name, sub_feed_titles in sub_titles.items():
        formatted_titles_list.append(f"{sub_name}: {', '.join(sub_feed_titles)}")
    formatted_titles_string = "\n".join(formatted_titles_list)

    template = prompt_template or "请用简洁的中文总结以下RSS订阅的标题内容，突出每组订阅的关键点，分组显示：\n\n{sub_titles}"
    return template.replace("{sub_titles}", formatted_titles_string)

def split_summary_text(summary_text, chunk_size=1000):
    text = summary_text or ''
    if not text:
        return ['暂无总结内容。']

    chunks = []
    current = ''

    def append_current():
        nonlocal current
        chunk = current.strip()
        if chunk:
            chunks.append(chunk)
        current = ''

    def append_piece(piece):
        nonlocal current
        if not piece:
            return
        separator = '\n\n' if current else ''
        if len(current) + len(separator) + len(piece) <= chunk_size:
            current = f"{current}{separator}{piece}" if current else piece
            return
        append_current()
        if len(piece) <= chunk_size:
            current = piece
            return
        for start in range(0, len(piece), chunk_size):
            sub_piece = piece[start:start + chunk_size].strip()
            if sub_piece:
                chunks.append(sub_piece)

    for paragraph in text.split('\n\n'):
        stripped_paragraph = paragraph.strip()
        if not stripped_paragraph:
            continue
        append_piece(stripped_paragraph)

    append_current()
    return chunks or ['暂无总结内容。']

def send_summary_bark_notification(device_key, title, summary_text):
    chunks = split_summary_text(summary_text)
    total_parts = len(chunks)
    responses = []
    aggregate_success = True

    for index, chunk in enumerate(chunks, start=1):
        part_title = title if total_parts == 1 else f"{title} ({index}/{total_parts})"
        success, response_data = send_bark_notification(
            device_key=device_key,
            title=part_title,
            body="",
            markdown=chunk,
            sound="glass",
            group="每日总结"
        )
        responses.append({
            'part': index,
            'total': total_parts,
            'title': part_title,
            'success': success,
            'response': response_data
        })
        if success:
            logger.info(f"总结 Bark 通知第 {index}/{total_parts} 部分发送成功。Message ID: {response_data.get('messageid', 'N/A')}")
        else:
            aggregate_success = False
            logger.error(f"总结 Bark 通知第 {index}/{total_parts} 部分发送失败。错误: {response_data.get('message', response_data.get('error', '未知错误'))}")

    return aggregate_success, {'parts': responses, 'total_parts': total_parts}

def extract_model_ids(models_response):
    model_entries = getattr(models_response, 'data', None) or models_response
    models = []
    for model in model_entries:
        model_id = getattr(model, 'id', None) or getattr(model, 'name', None)
        if not model_id and isinstance(model, dict):
            model_id = model.get('id') or model.get('name')
        if not model_id and isinstance(model, str):
            model_id = model
        if model_id:
            models.append(str(model_id))
    return sorted(set(models))

def generate_summary_with_provider(config_row, final_prompt):
    provider = normalize_ai_provider(config_row['ai_provider'])
    provider_name = get_provider_display_name(provider)
    model_name = get_summary_model_name(config_row, provider)
    api_key = get_summary_api_key(config_row, provider)

    if not api_key:
        raise ValueError(f"未配置 {provider_name} API Key")

    if provider == 'openai':
        if not OpenAI:
            raise RuntimeError("OpenAI SDK 未加载，无法生成总结。请安装 openai 依赖。")
        client = OpenAI(**build_openai_client_kwargs(api_key, get_openai_base_url(config_row)))
        response = client.chat.completions.create(
            model=model_name,
            messages=[{'role': 'user', 'content': final_prompt}],
            temperature=0.3
        )
        summary_text = response.choices[0].message.content if response.choices else ''
    else:
        if not genai or not types:
            raise RuntimeError("Gemini SDK 未加载，无法生成总结。请安装 google-genai 依赖。")
        client = genai.Client(api_key=api_key)
        grounding_tool = types.Tool(google_search=types.GoogleSearch())
        config = types.GenerateContentConfig(tools=[grounding_tool])
        response = client.models.generate_content(
            model=model_name,
            contents=final_prompt,
            config=config,
        )
        summary_text = response.text

    if not summary_text:
        raise RuntimeError(f"{provider_name} 未返回总结内容。")

    logger.info(f"{provider_name} API 成功生成总结 (Model: {model_name})。\n")
    return summary_text, provider_name, model_name

def update_summary_config(ai_provider, gemini_api_key, gemini_model, openai_api_key, openai_base_url, openai_model, prompt, interval_hours, summary_bark_key):
    try:
        normalized_provider = normalize_ai_provider(ai_provider)
        with get_db_connection() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO summary_config (id, interval_hours) 
                   VALUES (1, 24)"""
            )
            conn.execute(
                """UPDATE summary_config
                   SET ai_provider = ?,
                       gemini_api_key = ?,
                       gemini_model = ?,
                       openai_api_key = ?,
                       openai_base_url = ?,
                       openai_model = ?,
                       summary_prompt = ?,
                       interval_hours = ?,
                       summary_bark_key = ?
                   WHERE id = 1""",
                (normalized_provider, gemini_api_key, gemini_model, openai_api_key, openai_base_url, openai_model, prompt, interval_hours, summary_bark_key)
            )
            conn.commit()
            logger.info(f"总结配置已更新: Provider: {normalized_provider}, Gemini Key (已设置: {'是' if gemini_api_key else '否'}), Gemini Model: {gemini_model}, OpenAI Key (已设置: {'是' if openai_api_key else '否'}), OpenAI Base URL: {openai_base_url or '默认'}, OpenAI Model: {openai_model}, Interval: {interval_hours}h, Bark Key (已设置: {'是' if summary_bark_key else '否'})")
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

# --- MQTT, RSS Processing and Bark Notification ---

def send_mqtt_notification(payload, is_test=False):
    if not mqtt_publish:
        logger.warning("MQTT 库 (paho-mqtt) 未加载，跳过 MQTT 推送。\n")
        return False

    mqtt_config = get_mqtt_config()
    if not mqtt_config or not mqtt_config['enabled']:
        if not is_test:
            return True # Not enabled is not an error in normal flow
        # If it is a test, we need to inform the user it's not enabled.
        logger.warning("尝试测试 MQTT 但其未在配置中启用。\n")
        return False

    if not all([mqtt_config['host'], mqtt_config['port'], mqtt_config['topic']]):
        logger.warning("MQTT 已启用但配置不完整 (主机、端口或主题缺失)，跳过推送。\n")
        return False

    auth = None
    if mqtt_config['username']:
        auth = {'username': mqtt_config['username'], 'password': mqtt_config['password']}

    try:
        logger.info(f"准备发送 MQTT 消息到主题 '{mqtt_config['topic']}'...\n")
        mqtt_publish.single(
            topic=mqtt_config['topic'],
            payload=json.dumps(payload, ensure_ascii=False),
            hostname=mqtt_config['host'],
            port=mqtt_config['port'],
            auth=auth,
            qos=1,
            retain=False
        )
        logger.info("MQTT 消息发送成功。\n")
        return True
    except Exception as e:
        logger.error(f"发送 MQTT 消息失败: {e}", exc_info=True)
        return False

async def fetch_feed_content(url, sub_id):
    headers = {'User-Agent': f'RSS-to-Bark-Pusher/1.5 (sub_id:{sub_id})'}
    timeout_config = aiohttp.ClientTimeout(total=FEED_REQUEST_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout_config) as session:
        try:
            async with session.get(url, headers=headers) as response:
                response.raise_for_status()
                content = await response.read()
                logger.debug(f"成功获取订阅内容，字节数: {len(content)}")
                return content, None
        except asyncio.TimeoutError:
            error_reason = f"请求超时（超过 {FEED_REQUEST_TIMEOUT} 秒）"
            logger.error(f"抓取订阅 {url} 超时 (超过 {FEED_REQUEST_TIMEOUT} 秒)。")
            return None, error_reason
        except aiohttp.ClientResponseError as e:
            error_reason = f"HTTP {e.status}: {e.message}"
            logger.error(f"抓取订阅 {url} 时发生 HTTP 错误: {e.status} {e.message}")
            return None, error_reason
        except aiohttp.ClientError as e:
            error_reason = f"请求失败: {e}"
            logger.error(f"抓取订阅 {url} 失败: {e}")
            return None, error_reason

def should_send_failure_warning(consecutive_failures, failure_notified_at, now_utc):
    if consecutive_failures < FEED_FAILURE_WARNING_THRESHOLD:
        return False
    if failure_notified_at is None:
        return True
    reminder_interval = timedelta(hours=FEED_FAILURE_REMINDER_HOURS)
    return now_utc - failure_notified_at >= reminder_interval

def mark_subscription_failure_notified(sub_id, notified_at):
    try:
        with get_db_connection() as conn:
            conn.execute(
                "UPDATE subscriptions SET failure_notified_at = ? WHERE id = ?",
                (notified_at, sub_id)
            )
            conn.commit()
    except sqlite3.Error as e:
        logger.error(f"更新订阅 {sub_id} 的失败提醒时间时发生数据库错误: {e}")

def handle_feed_fetch_failure(sub, error_reason):
    now_utc = datetime.now(timezone.utc)
    consecutive_failures = (sub['consecutive_failures'] or 0) + 1

    try:
        with get_db_connection() as conn:
            conn.execute(
                """UPDATE subscriptions
                   SET consecutive_failures = ?,
                       last_failure_at = ?,
                       last_failure_reason = ?
                   WHERE id = ?""",
                (consecutive_failures, now_utc, error_reason, sub['id'])
            )
            conn.commit()
    except sqlite3.Error as e:
        logger.error(f"记录订阅 {sub['name']} 抓取失败状态时发生数据库错误: {e}")
        return

    logger.info(f"订阅 {sub['name']} 已连续抓取失败 {consecutive_failures} 次。原因: {error_reason}")

    if not should_send_failure_warning(consecutive_failures, sub['failure_notified_at'], now_utc):
        return

    title = f"RSS订阅抓取异常：{sub['name']}"
    body = (
        f"订阅已连续抓取失败 {consecutive_failures} 次。\n"
        f"原因：{error_reason}\n"
        f"地址：{sub['url']}\n"
        f"后续仍失败时，每 {FEED_FAILURE_REMINDER_HOURS} 小时最多提醒一次；恢复后会自动通知。"
    )
    success, response_data = send_bark_notification(
        device_key=sub['bark_key'],
        title=title,
        body=body[:500],
        url=sub['url'],
        sound="glass",
        group=f"订阅异常-{sub['name']}"
    )
    if success:
        mark_subscription_failure_notified(sub['id'], now_utc)
        logger.info(f"订阅 {sub['name']} 抓取异常 Bark 提醒已发送。")
    else:
        logger.error(f"订阅 {sub['name']} 抓取异常 Bark 提醒发送失败: {response_data.get('message', '未知错误')}")

def reset_subscription_failure_state_if_needed(sub):
    consecutive_failures = sub['consecutive_failures'] or 0
    failure_notified_at = sub['failure_notified_at']
    if consecutive_failures == 0 and failure_notified_at is None:
        return

    should_send_recovery = failure_notified_at is not None
    try:
        with get_db_connection() as conn:
            conn.execute(
                """UPDATE subscriptions
                   SET consecutive_failures = 0,
                       last_failure_at = NULL,
                       last_failure_reason = NULL,
                       failure_notified_at = NULL
                   WHERE id = ?""",
                (sub['id'],)
            )
            conn.commit()
    except sqlite3.Error as e:
        logger.error(f"重置订阅 {sub['name']} 抓取失败状态时发生数据库错误: {e}")
        return

    logger.info(f"订阅 {sub['name']} 抓取已恢复，失败状态已清零。")
    if not should_send_recovery:
        return

    title = f"RSS订阅已恢复：{sub['name']}"
    body = (
        f"订阅抓取已恢复正常。\n"
        f"此前连续失败次数：{consecutive_failures}\n"
        f"地址：{sub['url']}"
    )
    success, response_data = send_bark_notification(
        device_key=sub['bark_key'],
        title=title,
        body=body[:500],
        url=sub['url'],
        sound="glass",
        group=f"订阅异常-{sub['name']}"
    )
    if success:
        logger.info(f"订阅 {sub['name']} 恢复 Bark 提醒已发送。")
    else:
        logger.error(f"订阅 {sub['name']} 恢复 Bark 提醒发送失败: {response_data.get('message', '未知错误')}")

def process_feed(subscription_id, is_test_run=False):
    sub = get_subscription_by_id(subscription_id)
    if not sub:
        logger.warning(f"订阅 {subscription_id} 在 process_feed 中未找到，跳过处理。\n")
        return
    bark_level = normalize_bark_level(sub['bark_level'] if 'bark_level' in sub.keys() else '')
    
    logger.info(f"开始处理订阅: {sub['name']} ({sub['url']}) (Active: {sub['is_active']}, Test: {is_test_run})")
    update_subscription_last_checked(sub['id'])

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    feed_content, fetch_error = loop.run_until_complete(fetch_feed_content(sub['url'], sub['id']))
    loop.close()

    if feed_content is None:
        if not is_test_run:
            handle_feed_fetch_failure(sub, fetch_error or "未知抓取错误")
        return

    if not is_test_run:
        reset_subscription_failure_state_if_needed(sub)

    feed_data = feedparser.parse(feed_content)
    if feed_data.bozo:
        bozo_exc_type = type(feed_data.bozo_exception).__name__
        bozo_exc_msg = str(feed_data.bozo_exception)
        logger.warning(f"订阅 {sub['name']} ({sub['url']}) 格式可能不正确: {bozo_exc_type} - {bozo_exc_msg}")
    
    if not feed_data.entries:
        logger.info(f"订阅 {sub['name']} ({sub['url']}) 没有条目。\n")
        return

    def get_entry_link(entry):
        return entry.get('link')

    new_items_to_notify = []
    try:
        with get_db_connection() as conn:
            # 倒序遍历条目（从最旧到最新），以保持通知的正确时间顺序
            for entry in reversed(feed_data.entries):
                title = entry.get('title', '无标题')
                link = get_entry_link(entry)
                
                # 尝试插入。如果 link 是唯一的，插入会成功，说明是新条目。
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO feed_items (subscription_id, title, link, fetched_at) VALUES (?, ?, ?, ?)",
                    (sub['id'], title, link, datetime.now(timezone.utc)) 
                )
                
                # 如果 rowcount 是 1，表示插入成功（即，这是一个新条目）
                if cursor.rowcount == 1:
                    new_items_to_notify.append(entry)
            
            conn.commit()
    except sqlite3.Error as e:
        logger.error(f"在为订阅 {sub['name']} 保存和检查条目时发生数据库错误: {e}")
        return # 如果数据库操作失败，则中止

    # 如果是首次运行且检测到多个新条目，则只通知最新的一条以防刷屏
    # 'last_fetched_item_link' 字段现在作为一个标志，判断是否为首次运行
    if not sub['last_fetched_item_link'] and not is_test_run and len(new_items_to_notify) > 1:
        logger.info(f"首次运行 {sub['name']} 检测到 {len(new_items_to_notify)} 个新条目。为避免刷屏，仅处理最新的一条。")
        new_items_to_notify = [new_items_to_notify[-1]]

    if not sub['is_active'] and not is_test_run:
        logger.info(f"订阅 {sub['name']} 未激活，开始检查关键词触发。\n")
        keyword_triggers = get_all_keyword_triggers()
        
        if new_items_to_notify and keyword_triggers:
            notified_links_this_run = set()
            for item in new_items_to_notify:
                item_title = item.get('title', '无标题')
                item_link = get_entry_link(item)
                
                if item_link in notified_links_this_run:
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
                        
                        success, _ = send_bark_notification(
                            device_key=trigger['bark_key'],
                            title=notification_title,
                            body=body_content[:500],
                            url=link if link else None,
                            sound="glass",
                            group=f"关键词-{sub['name']}"
                        )
                        if success:
                            mqtt_payload = {
                                'source': 'keyword_trigger',
                                'subscription_name': sub['name'],
                                'keyword': keyword,
                                'title': item_title,
                                'body': body_content[:500],
                                'link': link
                            }
                            send_mqtt_notification(mqtt_payload)

                        notified_links_this_run.add(item_link)
                        break
        
        if feed_data.entries:
            latest_entry_link = get_entry_link(feed_data.entries[0])
            if sub['last_fetched_item_link'] != latest_entry_link:
                 update_subscription_last_item_link(sub['id'], latest_entry_link)
                 logger.info(f"非激活订阅 {sub['name']} 更新了 last_fetched_item_link 为: {latest_entry_link}")
        return

    items_for_active_or_test = []
    if is_test_run:
        if feed_data.entries:
            items_for_active_or_test.append(feed_data.entries[0])
            logger.info(f"[测试模式] 为 {sub['name']} 准备推送最新条目: {feed_data.entries[0].get('title', '无标题')}")
        else:
            logger.info(f"[测试模式] 订阅 {sub['name']} 没有条目可供测试。\n")
            return
    else:
        items_for_active_or_test = new_items_to_notify

    if not items_for_active_or_test:
        logger.info(f"订阅 {sub['name']} 没有新内容。\n")
        if feed_data.entries and not is_test_run:
            latest_entry_link = get_entry_link(feed_data.entries[0])
            if sub['last_fetched_item_link'] != latest_entry_link:
                update_subscription_last_item_link(sub['id'], latest_entry_link)
                logger.info(f"订阅 {sub['name']} 没有新通知内容，但更新了 last_fetched_item_link 为: {latest_entry_link}")
        return

    latest_sent_link_this_run = None
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
            group=sub['name'],
            level=bark_level
        )

        if success_flag:
            logger.info(f"Bark 通知发送成功 for '{title}'. Message ID: {response_data.get('messageid', 'N/A')}")
            if not is_test_run:
                latest_sent_link_this_run = get_entry_link(item)
            
            mqtt_payload = {
                'source': 'single_item',
                'subscription_name': sub['name'],
                'title': title,
                'body': body_content[:500],
                'link': link,
                'is_test': is_test_run
            }
            send_mqtt_notification(mqtt_payload)
        else:
            logger.error(f"Bark 通知发送失败 for '{title}'. 错误: {response_data.get('message', '未知错误')}")

    elif len(items_for_active_or_test) > 1:
        num_new_items = len(items_for_active_or_test)
        aggregated_title = f"{effective_title_prefix}{sub['name']} 有 {num_new_items} 条新更新"
        
        items_payload = []
        for item in items_for_active_or_test:
            items_payload.append({
                'title': item.get('title', '无标题'),
                'link': item.get('link', '')
            })

        aggregated_markdown = "\n".join([f"{i+1}. [{item['title']}]({item['link']})" for i, item in enumerate(items_payload)])
        primary_url_for_notification = items_for_active_or_test[-1].get('link', sub['url'])

        logger.info(f"准备发送 Bark 通知 (聚合 {num_new_items} 条目): Feed='{sub['name']}' (Test: {is_test_run})")
        success_flag, response_data = send_bark_notification(
            device_key=sub['bark_key'],
            title=aggregated_title,
            body="",
            markdown=aggregated_markdown[:2000],
            url=primary_url_for_notification,
            sound="glass",
            group=sub['name'],
            level=bark_level
        )

        if success_flag:
            logger.info(f"聚合 Bark 通知发送成功 for {num_new_items} items from '{sub['name']}'. Message ID: {response_data.get('messageid', 'N/A')}")
            if not is_test_run:
                latest_item_in_batch = items_for_active_or_test[-1]
                latest_sent_link_this_run = get_entry_link(latest_item_in_batch)

            # For aggregated notifications, send MQTT for each item individually
            logger.info(f"聚合 Bark 通知成功后，为 {num_new_items} 个条目单独发送 MQTT 通知。")
            for item in items_for_active_or_test:
                title = item.get('title', '无标题')
                link = item.get('link', '')
                raw_summary = item.get('summary', item.get('description', title))
                soup = BeautifulSoup(raw_summary, "html.parser")
                body_content = soup.get_text(separator=' ', strip=True)

                mqtt_payload = {
                    'source': 'single_item_from_aggregated',
                    'subscription_name': sub['name'],
                    'title': title,
                    'body': body_content[:500],
                    'link': link,
                    'is_test': is_test_run
                }
                send_mqtt_notification(mqtt_payload)
        else:
            logger.error(f"聚合 Bark 通知发送失败 for '{sub['name']}'. 错误: {response_data.get('message', '未知错误')}")

    if latest_sent_link_this_run and not is_test_run:
        update_subscription_last_item_link(sub['id'], latest_sent_link_this_run)
        logger.info(f"更新订阅 {sub['name']} 的 last_fetched_item_link 为: {latest_sent_link_this_run}")
    elif is_test_run and items_for_active_or_test:
        logger.info(f"[测试模式] 订阅 {sub['name']} 的测试通知已尝试发送。last_fetched_item_link 未更新。\n")

    del feed_data
    gc.collect()

# --- AI Summary Processing ---
def generate_daily_summary():
    config_row = get_summary_config()
    if not config_row:
        logger.warning("未配置总结参数，跳过每日总结。\n")
        return
    provider = normalize_ai_provider(config_row['ai_provider'])
    provider_name = get_provider_display_name(provider)
    if not get_summary_api_key(config_row, provider):
        logger.warning(f"未配置 {provider_name} API Key，跳过每日总结。\n")
        return
    if not config_row['summary_bark_key']:
        logger.warning("未配置总结 Bark Key，跳过每日总结通知。\n")
        return
    if provider == 'gemini' and (not genai or not types):
        logger.error("Gemini SDK 未加载，无法生成总结。\n")
        return
    if provider == 'openai' and not OpenAI:
        logger.error("OpenAI SDK 未加载，无法生成总结。请安装 openai 依赖。\n")
        return

    titles = get_daily_feed_titles()
    interval_hours = config_row['interval_hours'] if config_row['interval_hours'] is not None else 24
    if not titles:
        logger.info(f"过去{interval_hours}小时内没有新订阅标题，跳过总结。\n")
        return

    final_prompt = build_summary_prompt(titles, config_row['summary_prompt'])

    try:
        summary_text, provider_name, model_name = generate_summary_with_provider(config_row, final_prompt)
        save_summary_result(summary_text)

        success, response_data = send_summary_bark_notification(
            device_key=config_row['summary_bark_key'],
            title="每日RSS总结",
            summary_text=summary_text
        )
        if success:
            logger.info(f"每日总结 Bark 通知发送成功，共 {response_data.get('total_parts', 0)} 部分。")
            mqtt_payload = {
                'source': 'daily_summary',
                'title': "每日RSS总结",
                'body': summary_text[:2000]
            }
            send_mqtt_notification(mqtt_payload)
        else:
            logger.error(f"每日总结 Bark 通知发送失败。部分响应: {response_data}")
    except ClientError as e:
        if e.code == 429:
             logger.warning(f"{provider_name} API 限额已达 (429). 请检查配额或稍后再试。错误信息: {e.message}")
        else:
             logger.error(f"调用 {provider_name} API 失败 (ClientError): {e}", exc_info=True)
    except Exception as e:
        logger.error(f"调用 {provider_name} API 生成总结失败: {e}", exc_info=True)
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
        logger.debug(f"任务 {job_id} 未找到，无需移除。\n")
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
        logger.info("未配置总结间隔或总结间隔为0，跳过总结任务调度。\n")
        return
    if config_row['interval_hours'] < 1:
        logger.warning(f"总结间隔配置为 {config_row['interval_hours']} 小时，至少应为1小时。跳过总结任务调度。\n")
        return

    job_id = "daily_summary"
    try:
        existing_job = scheduler.get_job(job_id)
        if existing_job:
            scheduler.remove_job(job_id)
            logger.info(f"已移除现有总结任务: {job_id}\n")
    except JobLookupError:
        logger.debug("总结任务未找到，无需移除。\n")
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
            logger.info(f"已移除现有清理任务: {job_id}\n")
    except JobLookupError:
        logger.debug("清理任务未找到，无需移除。\n")
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
    logger.info("重新加载并调度所有订阅任务和总结任务...\n")
    try:
        subscriptions = get_all_subscriptions()
    except Exception as e:
        logger.error(f"重新调度任务时无法获取订阅列表: {e}", exc_info=True)
        return

    for job in scheduler.get_jobs():
        if job.id.startswith("feed_") or job.id == "daily_summary" or job.id == "cleanup_feed_items":
            try:
                scheduler.remove_job(job.id)
                logger.info(f"已移除旧任务: {job.id} (在重新调度所有任务前)\n")
            except JobLookupError:
                pass
            except Exception as e:
                logger.error(f"移除旧任务 {job.id} 时发生错误: {e}", exc_info=True)

    for sub in subscriptions:
        schedule_feed_job(sub)

    schedule_summary_job()
    schedule_cleanup_job()
    logger.info("所有任务重新调度完成。\n")

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
    bark_level = normalize_bark_level(request.form.get('bark_level', ''))

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

    sub_id = add_sub_to_db(name, url, interval_minutes, bark_key, bark_level)
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
    bark_level = normalize_bark_level(request.form.get('bark_level', ''))

    current_form_data = dict(original_sub)
    current_form_data.update({
        'name': name,
        'url': url_new,
        'interval_minutes': interval_minutes_str,
        'bark_key': bark_key,
        'bark_level': bark_level
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

    success_db_update = update_subscription_details_in_db(sub_id, name, url_new, interval_minutes, bark_key, bark_level)
    
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
            logger.info(f"已移除任务: {job_id} (因删除订阅)\n")
        except JobLookupError:
            logger.info(f"任务 {job_id} 未找到，可能已被移除或未调度 (删除订阅时)。\n" )
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

@app.route('/mqtt_config', methods=['GET', 'POST'])
def mqtt_config():
    if request.method == 'POST':
        enabled = 'mqtt_enabled' in request.form
        host = request.form.get('mqtt_host', '').strip()
        port_str = request.form.get('mqtt_port', '1883').strip()
        topic = request.form.get('mqtt_topic', '').strip()
        username = request.form.get('mqtt_username', '').strip()
        password = request.form.get('mqtt_password', '').strip()

        try:
            port = int(port_str)
        except (ValueError, TypeError):
            flash('端口必须是有效的数字。', 'error')
            # Re-render with current (invalid) data
            config_data = {
                'enabled': enabled, 'host': host, 'port': port_str,
                'topic': topic, 'username': username, 'password': password
            }
            return render_template('mqtt_config.html', config=config_data)

        if enabled and not all([host, port, topic]):
            flash('启用 MQTT 时，主机、端口和主题字段都是必填的。', 'error')
            config_data = {
                'enabled': enabled, 'host': host, 'port': port,
                'topic': topic, 'username': username, 'password': password
            }
            return render_template('mqtt_config.html', config=config_data)

        if save_mqtt_config(enabled, host, port, topic, username, password):
            flash('MQTT 配置已成功保存。', 'success')
        else:
            flash('保存 MQTT 配置时发生错误，请检查日志。', 'error')
        
        return redirect(url_for('index'))

    config = get_mqtt_config()
    return render_template('mqtt_config.html', config=config or {})

@app.route('/test_mqtt')
def test_mqtt():
    test_payload = {
        'source': 'test_button',
        'title': 'MQTT 连接测试',
        'body': f'这是一条来自 RSS Bark Pusher 的测试消息。发送时间: {datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")}',
        'is_test': True
    }
    if send_mqtt_notification(test_payload, is_test=True):
        flash('MQTT 测试消息已成功发送，请检查您的 MQTT 客户端是否收到消息。', 'success')
    else:
        flash('MQTT 测试消息发送失败。请检查配置是否正确、MQTT服务是否启用，并查看应用日志获取详细信息。', 'error')
    return redirect(url_for('mqtt_config'))

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
    csrf_token = get_summary_config_csrf_token()
    
    if db_config_row:
        current_config_dict = dict(db_config_row)
    else:
        current_config_dict = {
            'id': 1, 'ai_provider': 'gemini', 'gemini_api_key': None, 'gemini_model': None,
            'openai_api_key': None, 'openai_base_url': None, 'openai_model': None, 'summary_prompt': None,
            'interval_hours': 24, 'summary_bark_key': None, 
            'last_summary': None, 'last_summary_at': None
        }
    current_config_dict['ai_provider'] = normalize_ai_provider(current_config_dict.get('ai_provider'))
    if not current_config_dict.get('gemini_model'):
        current_config_dict['gemini_model'] = os.environ.get('GEMINI_MODEL_NAME', 'gemini-2.5-flash')
    if not current_config_dict.get('openai_model'):
        current_config_dict['openai_model'] = os.environ.get('OPENAI_MODEL_NAME', 'gpt-4o-mini')

    if request.method == 'POST':
        if not validate_summary_config_csrf(request.form.get('csrf_token', '')):
            flash('表单已过期，请刷新页面后重试。', 'error')
            return redirect(url_for('summary_config'))

        db_gemini_key = current_config_dict.get('gemini_api_key', '')
        db_openai_key = current_config_dict.get('openai_api_key', '')
        db_summary_bark_key = current_config_dict.get('summary_bark_key', '')

        ai_provider_from_form = normalize_ai_provider(request.form.get('ai_provider', current_config_dict.get('ai_provider', 'gemini')))
        form_gemini_api_key_input = request.form.get('gemini_api_key', '').strip()
        form_openai_api_key_input = request.form.get('openai_api_key', '').strip()
        form_summary_bark_key_input = request.form.get('summary_bark_key', '').strip()
        
        actual_gemini_api_key = resolve_masked_secret(form_gemini_api_key_input, stored_value=db_gemini_key)
        actual_openai_api_key = resolve_masked_secret(form_openai_api_key_input, stored_value=db_openai_key)
        actual_summary_bark_key = resolve_masked_secret(form_summary_bark_key_input, stored_value=db_summary_bark_key)
        
        summary_prompt_from_form = request.form.get('summary_prompt', '').strip()
        gemini_model_from_form = request.form.get('gemini_model', '').strip()
        openai_base_url_from_form = request.form.get('openai_base_url', '').strip()
        openai_model_from_form = request.form.get('openai_model', '').strip()
        interval_hours_str_from_form = request.form.get('interval_hours', '24').strip()

        try:
            interval_hours_val = int(interval_hours_str_from_form)
            if interval_hours_val < 1:
                flash('总结间隔不能小于1小时。', 'error')
                form_data_for_render = current_config_dict.copy()
                form_data_for_render.update({
                    'ai_provider': ai_provider_from_form,
                    'gemini_api_key': actual_gemini_api_key, 
                    'gemini_model': gemini_model_from_form,
                    'openai_api_key': actual_openai_api_key,
                    'openai_base_url': openai_base_url_from_form,
                    'openai_model': openai_model_from_form,
                    'summary_prompt': summary_prompt_from_form,
                    'interval_hours': interval_hours_str_from_form,
                    'summary_bark_key': actual_summary_bark_key
                })
                return render_template('summary_config.html', config=form_data_for_render, show_items_area=False, show_summary_area=False, csrf_token=csrf_token)
        except ValueError:
            flash('总结间隔必须是有效的数字。', 'error')
            form_data_for_render = current_config_dict.copy()
            form_data_for_render.update({
                'ai_provider': ai_provider_from_form,
                'gemini_api_key': actual_gemini_api_key,
                'gemini_model': gemini_model_from_form,
                'openai_api_key': actual_openai_api_key,
                'openai_base_url': openai_base_url_from_form,
                'openai_model': openai_model_from_form,
                'summary_prompt': summary_prompt_from_form,
                'interval_hours': interval_hours_str_from_form,
                'summary_bark_key': actual_summary_bark_key
            })
            return render_template('summary_config.html', config=form_data_for_render, show_items_area=False, show_summary_area=False, csrf_token=csrf_token)

        if update_summary_config(ai_provider_from_form, actual_gemini_api_key, gemini_model_from_form, actual_openai_api_key, openai_base_url_from_form, openai_model_from_form, summary_prompt_from_form, interval_hours_val, actual_summary_bark_key):
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
                           show_summary_area=show_summary_area_flag,
                           csrf_token=csrf_token)

@app.route('/summary_config/models', methods=['POST'])
def summary_config_models():
    payload = request.get_json(silent=True) or request.form
    csrf_token = request.headers.get('X-CSRF-Token') or payload.get('csrf_token') or ''
    if not validate_summary_config_csrf(csrf_token):
        return jsonify({'success': False, 'message': '请求已过期，请刷新页面后重试。'}), 400

    provider = normalize_ai_provider(payload.get('provider', 'gemini'))
    db_config_row = get_summary_config()

    if provider == 'openai':
        if not OpenAI:
            return jsonify({'success': False, 'message': 'OpenAI SDK 未加载，无法获取模型列表。'}), 500

        api_key_input = (payload.get('openai_api_key') or '').strip()
        stored_api_key = db_config_row['openai_api_key'] if db_config_row and db_config_row['openai_api_key'] else ''
        api_key = stored_api_key if api_key_input == '********' or not api_key_input else api_key_input

        if not api_key:
            return jsonify({'success': False, 'message': '请先填写 OpenAI API Key。'}), 400

        has_base_url_payload = 'openai_base_url' in payload
        base_url_input = (payload.get('openai_base_url') or '').strip()
        stored_base_url = db_config_row['openai_base_url'] if db_config_row and db_config_row['openai_base_url'] else ''
        openai_base_url = base_url_input if has_base_url_payload else stored_base_url

        try:
            client = OpenAI(**build_openai_client_kwargs(api_key, openai_base_url))
            models = extract_model_ids(client.models.list())
            if not models:
                return jsonify({'success': False, 'message': '没有找到可用的 OpenAI 模型。'}), 404

            return jsonify({'success': True, 'models': models})
        except Exception as e:
            logger.error(f"获取 OpenAI 模型列表失败: {e}", exc_info=True)
            return jsonify({'success': False, 'message': '获取 OpenAI 模型列表失败，请检查 API Key、Base URL、网络或应用日志。'}), 502

    if not genai:
        return jsonify({'success': False, 'message': 'Gemini SDK 未加载，无法获取模型列表。'}), 500

    api_key_input = (payload.get('gemini_api_key') or '').strip()

    stored_api_key = db_config_row['gemini_api_key'] if db_config_row and db_config_row['gemini_api_key'] else ''

    if api_key_input == '********' or not api_key_input:
        api_key = stored_api_key
    else:
        api_key = api_key_input

    if not api_key:
        return jsonify({'success': False, 'message': '请先填写 Gemini API Key。'}), 400

    try:
        client = genai.Client(api_key=api_key)
        models = []
        for model in client.models.list():
            supported_actions = getattr(model, 'supported_actions', []) or []
            if 'generateContent' in supported_actions:
                models.append(model.name.replace('models/', ''))

        models = sorted(set(models))
        if not models:
            return jsonify({'success': False, 'message': '没有找到支持生成内容的 Gemini 模型。'}), 404

        return jsonify({'success': True, 'models': models})
    except ClientError as e:
        logger.warning(f"获取 Gemini 模型列表失败 (ClientError): {e}")
        return jsonify({'success': False, 'message': '获取 Gemini 模型列表失败，请检查 API Key、网络或应用日志。'}), 502
    except Exception as e:
        logger.error(f"获取 Gemini 模型列表失败: {e}", exc_info=True)
        return jsonify({'success': False, 'message': '获取 Gemini 模型列表失败，请检查 API Key、网络或应用日志。'}), 502

@app.route('/test_summary')
def test_summary():
    config_row = get_summary_config()
    if not config_row:
        flash('未配置总结参数，无法测试总结。', 'error')
        return redirect(url_for('summary_config'))
    provider = normalize_ai_provider(config_row['ai_provider'])
    provider_name = get_provider_display_name(provider)
    if not get_summary_api_key(config_row, provider):
        flash(f'未配置 {provider_name} API Key，无法测试总结。', 'error')
        return redirect(url_for('summary_config'))
    if not config_row['summary_bark_key']:
        flash('未配置总结 Bark Key，无法测试总结通知。', 'error')
        return redirect(url_for('summary_config'))
    if provider == 'gemini' and (not genai or not types):
        flash("Gemini SDK 未加载，无法测试总结。", "error")
        return redirect(url_for('summary_config'))
    if provider == 'openai' and not OpenAI:
        flash("OpenAI SDK 未加载，无法测试总结。请安装 openai 依赖。", "error")
        return redirect(url_for('summary_config'))

    titles = get_daily_feed_titles()
    interval_hours = config_row['interval_hours'] if config_row['interval_hours'] is not None else 24
    if not titles:
        flash(f'过去{interval_hours}小时内没有新订阅标题，无法生成测试总结。', 'info')
        return redirect(url_for('summary_config'))

    final_prompt = build_summary_prompt(titles, config_row['summary_prompt'])
    logger.info(f"测试总结使用的最终提示词: {final_prompt[:500]}...\n")

    try:
        summary_text, provider_name, model_name = generate_summary_with_provider(config_row, final_prompt)
        save_summary_result(summary_text)

        success, response_data = send_summary_bark_notification(
            device_key=config_row['summary_bark_key'],
            title="[测试] 每日RSS总结",
            summary_text=summary_text
        )
        if success:
            logger.info(f"测试总结 Bark 通知发送成功，共 {response_data.get('total_parts', 0)} 部分。")
            flash('测试总结已生成并发送，请检查Bark设备。总结结果已更新到页面。', 'success')
            
            mqtt_payload = {
                'source': 'test_summary',
                'title': "[测试] 每日RSS总结",
                'body': summary_text[:2000],
                'is_test': True
            }
            send_mqtt_notification(mqtt_payload)

            return redirect(url_for('summary_config', show_summary='true')) 
        else:
            logger.error(f"测试总结 Bark 通知发送失败。部分响应: {response_data}")
            flash('测试总结通知发送失败，请检查日志。总结结果仍会更新到页面。', 'warning')
            return redirect(url_for('summary_config', show_summary='true'))
    except ClientError as e:
        if e.code == 429:
            logger.warning(f"测试总结失败: {provider_name} API 限额已达 (429)。{e.message}")
            flash(f"{provider_name} API 限额已达，请稍后再试。详细信息已记录到日志。", 'warning')
            return redirect(url_for('summary_config', show_summary='true'))
        else:
            logger.error(f"测试总结失败 ({provider_name} ClientError): {e}", exc_info=True)
            flash(f"测试总结失败 ({provider_name} API Error): {e}", 'error')
    except Exception as e:
        logger.error(f"测试总结失败 ({provider_name}): {e}", exc_info=True)
        flash(f"测试总结失败: {e}", 'error')

    return redirect(url_for('summary_config'))

# --- 应用初始化和启动 ---
db_dir_path = os.path.dirname(DATABASE_FILE)
if not os.path.exists(db_dir_path):
    try:
        os.makedirs(db_dir_path)
        logger.info(f"数据目录 {db_dir_path} 已创建 (在 app.py 启动时)。\n")
    except OSError as e:
        logger.critical(f"无法创建数据目录 {db_dir_path}: {e}。应用可能无法正常工作。", exc_info=True)

if not os.path.exists(DATABASE_FILE):
    logger.info(f"数据库文件 {DATABASE_FILE} 未找到，正在调用 init_db()...\n")
    try:
        init_db()
    except Exception as e:
        logger.critical(f"首次初始化数据库失败: {e}. 应用可能无法启动。", exc_info=True)
else:
    logger.info(f"使用现有数据库 {DATABASE_FILE}。调用 init_db() 以确保表结构和WAL模式...\n")
    try:
        init_db()
    except Exception as e:
        logger.warning(f"检查/更新现有数据库时出错: {e}. 应用将尝试继续。", exc_info=True)

if not scheduler.running:
    try:
        scheduler.start(paused=False)
        logger.info("调度器已启动。\n")
        reschedule_all_jobs()
    except Exception as e:
        logger.critical(f"调度器启动失败: {e}", exc_info=True)
else:
    logger.warning("调度器已在运行，可能由Gunicorn --reload或多次导入触发。跳过重复启动。\n")

if __name__ == '__main__':
    logger.info("以开发模式启动 Flask 应用 (python app.py)...\n")
    
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

    logger.info("开发服务器正在关闭...\n")
    if scheduler.running:
        try:
            logger.info("正在关闭调度器...\n")
            scheduler.shutdown()
            logger.info("调度器已关闭。\n")
        except Exception as e:
            logger.error(f"关闭调度器时出错: {e}", exc_info=True)
