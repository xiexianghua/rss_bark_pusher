# database.py
import sqlite3
import os
import logging
import time
import threading
from datetime import datetime, timezone
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# --- 配置数据库文件路径 ---
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_SUBDIR = "data"
DATABASE_FILENAME = "subscriptions.db"
DATABASE_FILE = os.path.join(APP_DIR, DATA_SUBDIR, DATABASE_FILENAME)

# 添加一个线程锁来序列化关键数据库操作
_db_lock = threading.RLock()

def _convert_timestamp_to_datetime(val_bytes):
    """
    Converts a byte string from SQLite (expected ISO8601 format or similar)
    to a timezone-aware datetime object (UTC).
    """
    if not val_bytes:
        return None
    dt_str = val_bytes.decode('utf-8')
    try:
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        logger.warning(f"Warning: Could not parse timestamp string '{dt_str}' into datetime. Returning as string.")
        return dt_str

sqlite3.register_converter("timestamp", _convert_timestamp_to_datetime)

def _retry_on_locked(func, max_retries=3, base_delay=0.1):
    """
    重试机制装饰器，专门处理数据库锁定问题
    """
    for attempt in range(max_retries):
        try:
            return func()
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e).lower() and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)  # 指数退避
                logger.warning(f"数据库锁定，第 {attempt + 1} 次重试，等待 {delay:.2f} 秒...")
                time.sleep(delay)
                continue
            else:
                raise
        except Exception as e:
            raise
    
    # 如果所有重试都失败了
    raise sqlite3.OperationalError("数据库在多次重试后仍然锁定")

@contextmanager
def get_db_connection(timeout_seconds=30):
    """
    使用上下文管理器确保连接总是被正确关闭
    增加超时时间并添加重试机制
    """
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
                detect_types=sqlite3.PARSE_DECLTYPES
            )
            conn.row_factory = sqlite3.Row
            # 优化SQLite设置
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")  # 提高性能
            conn.execute("PRAGMA cache_size=10000;")     # 增加缓存
            conn.execute("PRAGMA temp_store=memory;")    # 临时存储在内存
            conn.execute("PRAGMA busy_timeout=30000;")   # 30秒busy timeout
            return conn
        
        # 使用重试机制创建连接
        conn = _retry_on_locked(_create_connection)
        yield conn
        
    except sqlite3.Error as e:
        logger.error(f"连接数据库 {DATABASE_FILE} 时出错: {e}")
        if conn:
            try:
                conn.rollback()
            except:
                pass
        raise
    finally:
        if conn:
            try:
                conn.close()
            except Exception as e:
                logger.error(f"关闭数据库连接时出错: {e}")

def init_db():
    """初始化数据库：创建表并持久启用WAL模式。"""
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

    with _db_lock:  # 确保初始化过程是线程安全的
        try:
            # 直接使用sqlite3.connect而不是上下文管理器，因为这是初始化
            conn = sqlite3.connect(DATABASE_FILE, timeout=30, detect_types=sqlite3.PARSE_DECLTYPES)
            conn.row_factory = sqlite3.Row

            # 持久启用WAL模式和其他优化
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA cache_size=10000;")
            conn.execute("PRAGMA temp_store=memory;")
            conn.execute("PRAGMA busy_timeout=30000;")
            
            mode_row = conn.execute("PRAGMA journal_mode;").fetchone()
            if mode_row and mode_row[0].lower() == 'wal':
                logger.info(f"数据库 {DATABASE_FILE} 的 WAL mode 已成功持久启用/确认。")
            else:
                logger.warning(f"尝试为数据库 {DATABASE_FILE} 启用 WAL mode，但当前为 {mode_row[0] if mode_row else '未知'}。")

            # 创建表
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
            conn.commit()

            if not db_already_exists:
                logger.info(f"数据库 {DATABASE_FILE} 已创建并初始化。")
            else:
                logger.info(f"现有数据库 {DATABASE_FILE} 已检查，表结构和WAL模式已确认。")

        except sqlite3.Error as e:
            logger.error(f"初始化或检查数据库 {DATABASE_FILE} 失败: {e}")
            raise
        finally:
            if 'conn' in locals():
                conn.close()

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    init_db()
    try:
        with get_db_connection() as conn:
            print(f"成功连接到数据库: {DATABASE_FILE}")
            mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
            print(f"当前 Journal Mode: {mode}")
            if mode.lower() != 'wal':
                print("警告: WAL模式未成功启用!")
    except Exception as e:
        print(f"直接运行 database.py 测试时出错: {e}")