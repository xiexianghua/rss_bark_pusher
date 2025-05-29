# database.py
import sqlite3
import os
import logging
import time
import threading
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# --- 配置数据库文件路径 ---
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_SUBDIR = "data"
DATABASE_FILENAME = "subscriptions.db"
DATABASE_FILE = os.path.join(APP_DIR, DATA_SUBDIR, DATABASE_FILENAME)

# 添加一个线程锁来序列化关键数据库操作
_db_lock = threading.RLock()

# --- 自定义 SQLite datetime 适配器和转换器 ---
def adapt_datetime(dt):
    """将 datetime 对象转换为 ISO 8601 字符串 (UTC)。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    elif dt.tzinfo != timezone.utc:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()

def convert_datetime(val_bytes):
    """将 SQLite 中的 ISO 8601 字符串转换为 timezone-aware datetime (UTC)。"""
    if not val_bytes:
        return None
    dt_str = val_bytes.decode('utf-8')
    try:
        try:
            dt = datetime.fromisoformat(dt_str)
        except ValueError:
            dt_str_iso = dt_str.replace(" ", "T")
            dt = datetime.fromisoformat(dt_str_iso)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        elif dt.tzinfo != timezone.utc:
             dt = dt.astimezone(timezone.utc)
        return dt
    except ValueError:
        logger.warning(f"无法解析时间戳字符串 '{dt_str}'，返回原字符串。")
        return dt_str

sqlite3.register_adapter(datetime, adapt_datetime)
sqlite3.register_converter("timestamp", convert_datetime)

def _retry_on_locked(func, max_retries=5, base_delay=0.2):
    for attempt in range(max_retries):
        try:
            return func()
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e).lower() and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                logger.warning(f"数据库锁定，第 {attempt + 1}/{max_retries-1} 次重试，等待 {delay:.2f} 秒...")
                time.sleep(delay)
                continue
            else:
                logger.error(f"数据库操作失败 (可能是锁定或其他OperationalError): {e}")
                raise
        except Exception as e:
            logger.error(f"执行数据库操作时发生非OperationalError错误: {e}")
            raise
    logger.error(f"数据库在 {max_retries} 次重试后仍然锁定或操作失败。")
    raise sqlite3.OperationalError("数据库在多次重试后仍然锁定或操作失败")

@contextmanager
def get_db_connection(timeout_seconds=30):
    db_dir = os.path.dirname(DATABASE_FILE)
    if not os.path.exists(db_dir):
        try:
            os.makedirs(db_dir)
            logger.info(f"数据库目录 {db_dir} 已创建。")
        except OSError as e:
            logger.error(f"创建数据库目录 {db_dir} 失败: {e}")
            raise

    conn = None
    try:
        def _create_connection():
            nonlocal conn
            conn = sqlite3.connect(
                DATABASE_FILE, 
                timeout=timeout_seconds, 
                detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.execute("PRAGMA busy_timeout=30000;")
            return conn
        
        conn = _retry_on_locked(_create_connection)
        yield conn
        
    except sqlite3.Error as e:
        logger.error(f"连接或操作数据库 {DATABASE_FILE} 时出错: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception as rb_err:
                logger.error(f"数据库回滚失败: {rb_err}")
        raise
    finally:
        if conn:
            try:
                conn.close()
            except Exception as e:
                logger.error(f"关闭数据库连接时出错: {e}")

def cleanup_old_feed_items(days=1):  # 缩短到 1 天
    try:
        with get_db_connection() as conn:
            cutoff_time = datetime.now(timezone.utc) - timedelta(days=days)
            result = conn.execute(
                "DELETE FROM feed_items WHERE fetched_at < ?",
                (cutoff_time,)
            )
            conn.commit()
            deleted_count = result.rowcount
            logger.info(f"清理了 {deleted_count} 条超过 {days} 天的 feed_items 记录。")
            return deleted_count
    except sqlite3.Error as e:
        logger.error(f"清理旧 feed_items 记录时发生数据库错误: {e}")
        return 0

def _add_column_if_not_exists(conn, table_name, column_name, column_type):
    """辅助函数：如果列不存在，则向表中添加列"""
    cursor = conn.execute(f"PRAGMA table_info({table_name})")
    columns = [row['name'] for row in cursor.fetchall()]
    if column_name not in columns:
        try:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
            logger.info(f"已向表 '{table_name}' 添加列 '{column_name} {column_type}'。")
        except sqlite3.OperationalError as e:
            logger.warning(f"尝试向表 '{table_name}' 添加列 '{column_name}' 时发生错误 (可能已存在): {e}")
    else:
        logger.debug(f"列 '{column_name}' 已存在于表 '{table_name}'。")

def init_db():
    logger.info(f"正在初始化数据库: {DATABASE_FILE}")
    db_dir = os.path.dirname(DATABASE_FILE)
    if not os.path.exists(db_dir):
        try:
            os.makedirs(db_dir)
            logger.info(f"数据库目录 {db_dir} 已创建 (在 init_db 中)。")
        except OSError as e:
            logger.error(f"创建数据库目录 {db_dir} 失败 (在 init_db 中): {e}")
            return

    db_already_exists = os.path.exists(DATABASE_FILE)

    with _db_lock:
        conn = None
        try:
            conn = sqlite3.connect(DATABASE_FILE, timeout=30, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
            conn.row_factory = sqlite3.Row

            try:
                conn.execute("PRAGMA journal_mode=WAL;")
                mode_row = conn.execute("PRAGMA journal_mode;").fetchone()
                if mode_row and mode_row[0].lower() == 'wal':
                    logger.info(f"数据库 {DATABASE_FILE} 的 WAL mode 已成功启用/确认。")
                else:
                    logger.warning(f"尝试为数据库 {DATABASE_FILE} 启用 WAL mode，但当前为 {mode_row[0] if mode_row else '未知'}。")
            except sqlite3.Error as wal_err:
                logger.warning(f"设置 WAL mode 时出错 (可能是因为数据库已打开或存在活动事务): {wal_err}")

            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.execute("PRAGMA busy_timeout=30000;")
            
            # 创建 subscriptions 表
            conn.execute('''
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL UNIQUE,
                    interval_minutes INTEGER NOT NULL DEFAULT 60,
                    bark_key TEXT NOT NULL,
                    last_fetched_item_guid TEXT,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, 
                    last_checked_at TIMESTAMP
                )
            ''')

            # 创建 feed_items 表
            conn.execute('''
                CREATE TABLE IF NOT EXISTS feed_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    guid TEXT NOT NULL UNIQUE,
                    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE
                )
            ''')
            conn.execute("CREATE INDEX IF NOT EXISTS idx_feed_items_subscription_id ON feed_items(subscription_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_feed_items_fetched_at ON feed_items(fetched_at);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_feed_items_guid ON feed_items(guid);")  # 新增索引

            # 创建 summary_config 表
            conn.execute('''
                CREATE TABLE IF NOT EXISTS summary_config (
                    id INTEGER PRIMARY KEY,
                    gemini_api_key TEXT,
                    summary_prompt TEXT,
                    interval_hours INTEGER DEFAULT 24,
                    summary_bark_key TEXT, 
                    last_summary TEXT,
                    last_summary_at TIMESTAMP
                )
            ''')

            # Schema migration for summary_config table
            logger.info("正在检查并迁移 summary_config 表结构...")
            _add_column_if_not_exists(conn, "summary_config", "gemini_api_key", "TEXT")
            _add_column_if_not_exists(conn, "summary_config", "summary_prompt", "TEXT")
            _add_column_if_not_exists(conn, "summary_config", "summary_bark_key", "TEXT")
            _add_column_if_not_exists(conn, "summary_config", "last_summary", "TEXT")
            _add_column_if_not_exists(conn, "summary_config", "last_summary_at", "TIMESTAMP")

            # 插入默认 summary_config 记录
            conn.execute('''
                INSERT OR IGNORE INTO summary_config (id, interval_hours) 
                VALUES (1, 24)
            ''')
            conn.execute('''
                UPDATE summary_config SET interval_hours = COALESCE(interval_hours, 24) WHERE id = 1
            ''')

            conn.commit()

            if not db_already_exists:
                logger.info(f"数据库 {DATABASE_FILE} 已创建并初始化。")
            else:
                logger.info(f"现有数据库 {DATABASE_FILE} 已检查，表结构和 WAL 模式已确认/尝试设置，并已尝试进行必要的表结构迁移。")

        except sqlite3.Error as e:
            logger.error(f"初始化或检查数据库 {DATABASE_FILE} 失败: {e}", exc_info=True)
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise
        finally:
            if conn:
                conn.close()

if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
    logger.info("直接运行 database.py 进行测试...")
    
    if os.path.exists(DATABASE_FILE):
        logger.info(f"为测试目的，删除现有数据库文件: {DATABASE_FILE}")
        os.remove(DATABASE_FILE)
        for suffix in ["-wal", "-shm"]:
            wal_file = DATABASE_FILE + suffix
            if os.path.exists(wal_file):
                os.remove(wal_file)

    init_db()
    try:
        with get_db_connection() as conn_test:
            logger.info(f"成功连接到数据库: {DATABASE_FILE}")
            mode = conn_test.execute("PRAGMA journal_mode;").fetchone()[0]
            logger.info(f"当前 Journal Mode: {mode}")
            
            # 验证 summary_config 表结构
            logger.info("验证 summary_config 表结构:")
            cursor = conn_test.execute("PRAGMA table_info(summary_config)")
            columns = [row['name'] for row in cursor.fetchall()]
            expected_columns = ['id', 'gemini_api_key', 'summary_prompt', 'interval_hours', 'summary_bark_key', 'last_summary', 'last_summary_at']
            for col in expected_columns:
                assert col in columns, f"列 {col} 未在 summary_config 表中找到!"
            logger.info(f"summary_config 表列: {columns} - 验证通过")

            # 验证 summary_config 默认行和 interval_hours
            summary_row = conn_test.execute("SELECT * FROM summary_config WHERE id = 1").fetchone()
            assert summary_row is not None, "summary_config id=1 的默认行未找到"
            assert summary_row['interval_hours'] == 24, f"summary_config id=1 的 interval_hours ({summary_row['interval_hours']}) 不正确"
            logger.info("summary_config 默认行和 interval_hours 验证通过。")

            logger.info("测试时间戳转换...")
            conn_test.execute("DROP TABLE IF EXISTS test_datetime")
            conn_test.execute("CREATE TABLE test_datetime (id INTEGER PRIMARY KEY, ts TIMESTAMP)")
            now_utc = datetime.now(timezone.utc)
            conn_test.execute("INSERT INTO test_datetime (ts) VALUES (?)", (now_utc,))
            conn_test.commit()
            retrieved_row = conn_test.execute("SELECT ts FROM test_datetime").fetchone()
            if retrieved_row and isinstance(retrieved_row['ts'], datetime):
                logger.info(f"时间戳转换测试成功: 存储 {now_utc.isoformat()}, 取回 {retrieved_row['ts'].isoformat()} (类型: {type(retrieved_row['ts'])})")
                assert retrieved_row['ts'].tzinfo == timezone.utc
                assert now_utc.replace(microsecond=0) == retrieved_row['ts'].replace(microsecond=0)
            else:
                logger.error(f"时间戳转换测试失败: {retrieved_row}")
            conn_test.execute("DROP TABLE IF EXISTS test_datetime")
            conn_test.commit()

            logger.info("测试清理旧 feed_items...")
            sub_id = conn_test.execute("INSERT INTO subscriptions (name, url, bark_key) VALUES (?, ?, ?)", 
                                       ("Test Sub", "http://example.com/rss", "testkey")).lastrowid
            conn_test.commit()
            three_days_ago = datetime.now(timezone.utc) - timedelta(days=3)
            one_day_ago = datetime.now(timezone.utc) - timedelta(days=1)
            conn_test.execute("INSERT INTO feed_items (subscription_id, title, guid, fetched_at) VALUES (?, ?, ?, ?)",
                              (sub_id, "Old Item", "guid_old", three_days_ago))
            conn_test.execute("INSERT INTO feed_items (subscription_id, title, guid, fetched_at) VALUES (?, ?, ?, ?)",
                              (sub_id, "New Item", "guid_new", one_day_ago))
            conn_test.commit()
            deleted_count = cleanup_old_feed_items(days=1)
            assert deleted_count == 1, f"清理计数错误，应为1，实际为{deleted_count}"
            logger.info("feed_items 清理测试成功。")

    except Exception as e:
        logger.error(f"直接运行 database.py 测试时出错: {e}", exc_info=True)