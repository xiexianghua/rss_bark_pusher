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
        # Handle both space and 'T' separator for ISO 8601
        dt_str_iso = dt_str.replace(" ", "T")
        # Try parsing with timezone info first
        try:
            dt = datetime.fromisoformat(dt_str_iso)
        except ValueError:
            # If it fails, it might be naive, try parsing and then assume UTC
            # This might be needed for older CURRENT_TIMESTAMP formats
            dt_naive = datetime.strptime(dt_str_iso.split('.')[0], '%Y-%m-%dT%H:%M:%S')
            dt = dt_naive.replace(tzinfo=timezone.utc)

        if dt.tzinfo is None: # Should be redundant if fromisoformat worked with tz
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        logger.warning(f"Warning: Could not parse timestamp string '{dt_str}' into datetime. Returning as string.")
        return dt_str

sqlite3.register_converter("timestamp", _convert_timestamp_to_datetime)

def _retry_on_locked(func, max_retries=5, base_delay=0.2): # Increased retries and base_delay
    """
    重试机制装饰器，专门处理数据库锁定问题
    """
    for attempt in range(max_retries):
        try:
            return func()
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e).lower() and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)  # 指数退避
                logger.warning(f"数据库锁定，第 {attempt + 1}/{max_retries-1} 次重试，等待 {delay:.2f} 秒...")
                time.sleep(delay)
                continue
            else:
                logger.error(f"数据库操作失败 (可能是锁定或其他OperationalError): {e}")
                raise
        except Exception as e: # Catch other potential exceptions during func() execution
            logger.error(f"执行数据库操作时发生非OperationalError错误: {e}")
            raise
    
    # 如果所有重试都失败了
    logger.error(f"数据库在 {max_retries} 次重试后仍然锁定或操作失败。")
    raise sqlite3.OperationalError("数据库在多次重试后仍然锁定或操作失败")

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
            # The 'timestamp' converter is registered globally, so it applies here.
            conn = sqlite3.connect(
                DATABASE_FILE, 
                timeout=timeout_seconds, 
                detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES
            )
            conn.row_factory = sqlite3.Row
            # 优化SQLite设置 (这些在每次连接时设置是好的，特别是WAL)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;") # 如果有外键，建议开启
            conn.execute("PRAGMA busy_timeout=30000;") # 30秒busy timeout
            return conn
        
        conn = _retry_on_locked(_create_connection)
        yield conn
        
    except sqlite3.Error as e: # Catches errors from _retry_on_locked or other SQLite errors
        logger.error(f"连接或操作数据库 {DATABASE_FILE} 时出错: {e}")
        if conn:
            try:
                conn.rollback() # Attempt rollback on error
            except Exception as rb_err:
                logger.error(f"数据库回滚失败: {rb_err}")
        raise # Re-raise the original error
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
            return # Critical failure, cannot proceed

    db_already_exists = os.path.exists(DATABASE_FILE)

    # Using a lock here for init_db is good practice if it could be called concurrently,
    # though typically it's called once at startup.
    with _db_lock:
        conn = None # Ensure conn is defined for finally block
        try:
            # Connect directly for initialization, detect_types for custom converters
            conn = sqlite3.connect(DATABASE_FILE, timeout=30, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
            conn.row_factory = sqlite3.Row

            # Attempt to enable WAL mode. This is best-effort for existing DBs
            # if they weren't in WAL mode before. For new DBs, it will stick.
            try:
                conn.execute("PRAGMA journal_mode=WAL;")
                mode_row = conn.execute("PRAGMA journal_mode;").fetchone()
                if mode_row and mode_row[0].lower() == 'wal':
                    logger.info(f"数据库 {DATABASE_FILE} 的 WAL mode 已成功启用/确认。")
                else:
                    logger.warning(f"尝试为数据库 {DATABASE_FILE} 启用 WAL mode，但当前为 {mode_row[0] if mode_row else '未知'}。")
            except sqlite3.Error as wal_err:
                logger.warning(f"设置 WAL mode 时出错 (可能是因为数据库已打开或存在活动事务): {wal_err}")

            # Set other PRAGMAs that are generally good
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.execute("PRAGMA busy_timeout=30000;") # Good to have a busy timeout
            
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
            # Note: CURRENT_TIMESTAMP in SQLite stores as text "YYYY-MM-DD HH:MM:SS" (UTC by default in recent versions)
            # The converter handles this.
            conn.commit()

            if not db_already_exists:
                logger.info(f"数据库 {DATABASE_FILE} 已创建并初始化。")
            else:
                logger.info(f"现有数据库 {DATABASE_FILE} 已检查，表结构和 WAL 模式已确认/尝试设置。")

        except sqlite3.Error as e:
            logger.error(f"初始化或检查数据库 {DATABASE_FILE} 失败: {e}")
            if conn:
                try:
                    conn.rollback()
                except:
                    pass
            raise # Re-raise to signal failure of init_db
        finally:
            if conn:
                conn.close()

if __name__ == '__main__':
    # Setup basic logging for direct script execution
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
    logger.info("直接运行 database.py 进行测试...")
    init_db()
    try:
        with get_db_connection() as conn_test:
            logger.info(f"成功连接到数据库: {DATABASE_FILE}")
            mode = conn_test.execute("PRAGMA journal_mode;").fetchone()[0]
            logger.info(f"当前 Journal Mode: {mode}")
            if mode.lower() != 'wal':
                logger.warning("警告: WAL模式可能未成功启用 (可能是因为数据库当时正被其他连接使用)。")
            
            # Test inserting and retrieving a datetime
            conn_test.execute("DROP TABLE IF EXISTS test_datetime")
            conn_test.execute("CREATE TABLE test_datetime (id INTEGER PRIMARY KEY, ts TIMESTAMP)")
            
            now_utc = datetime.now(timezone.utc)
            conn_test.execute("INSERT INTO test_datetime (ts) VALUES (?)", (now_utc,))
            conn_test.commit()
            
            retrieved_row = conn_test.execute("SELECT ts FROM test_datetime").fetchone()
            if retrieved_row and isinstance(retrieved_row['ts'], datetime):
                logger.info(f"时间戳转换测试成功: 存储 {now_utc}, 取回 {retrieved_row['ts']} (类型: {type(retrieved_row['ts'])})")
                assert retrieved_row['ts'].tzinfo == timezone.utc, "Retrieved datetime is not UTC aware"
            else:
                logger.error(f"时间戳转换测试失败: {retrieved_row}")

            conn_test.execute("DROP TABLE IF EXISTS test_datetime")
            conn_test.commit()

    except Exception as e:
        logger.error(f"直接运行 database.py 测试时出错: {e}", exc_info=True)