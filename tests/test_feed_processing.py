import importlib
import os
import sqlite3
import sys
import tempfile
import types as module_types
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone


EASYTIER_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Release notes from EasyTier</title>
  <entry>
    <title>v2.6.4</title>
    <updated>2026-05-12T15:25:14Z</updated>
    <link rel="alternate" type="text/html" href="https://github.com/EasyTier/EasyTier/releases/tag/v2.6.4"/>
    <content type="html">latest</content>
  </entry>
  <entry>
    <title>v2.6.3</title>
    <updated>2026-05-02T04:29:11Z</updated>
    <link rel="alternate" type="text/html" href="https://github.com/EasyTier/EasyTier/releases/tag/v2.6.3"/>
    <content type="html">previous</content>
  </entry>
  <entry>
    <title>v2.6.2</title>
    <updated>2026-04-25T10:00:00Z</updated>
    <link rel="alternate" type="text/html" href="https://github.com/EasyTier/EasyTier/releases/tag/v2.6.2"/>
    <content type="html">old</content>
  </entry>
</feed>
"""


def create_schema(database_file):
    conn = sqlite3.connect(database_file)
    try:
        conn.execute(
            """
            CREATE TABLE subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                interval_minutes INTEGER NOT NULL DEFAULT 60,
                bark_key TEXT NOT NULL,
                bark_level TEXT NOT NULL DEFAULT '',
                last_fetched_item_link TEXT,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_checked_at TIMESTAMP,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                last_failure_at TIMESTAMP,
                last_failure_reason TEXT,
                failure_notified_at TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE feed_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                link TEXT NOT NULL UNIQUE,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE summary_config (
                id INTEGER PRIMARY KEY,
                interval_hours INTEGER DEFAULT 24
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE mqtt_config (
                id INTEGER PRIMARY KEY,
                enabled BOOLEAN NOT NULL DEFAULT 0,
                host TEXT,
                port INTEGER,
                topic TEXT,
                username TEXT,
                password TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def install_sqlite_database_module(database_file):
    @contextmanager
    def get_db_connection(*args, **kwargs):
        conn = sqlite3.connect(database_file)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    fake_database = module_types.ModuleType("database")
    fake_database.DATABASE_FILE = database_file
    fake_database._db_lock = module_types.SimpleNamespace(
        __enter__=lambda self: None,
        __exit__=lambda self, exc_type, exc, tb: None,
    )
    fake_database.get_db_connection = get_db_connection
    fake_database.init_db = lambda: None
    fake_database.cleanup_old_feed_items = lambda *args, **kwargs: 0
    fake_database.get_detailed_feed_items_for_summary = lambda *args, **kwargs: []
    fake_database.get_all_keyword_triggers = lambda: []
    fake_database.add_keyword_trigger = lambda *args, **kwargs: True
    fake_database.delete_keyword_trigger = lambda *args, **kwargs: True
    fake_database.get_mqtt_config = lambda: None
    fake_database.save_mqtt_config = lambda *args, **kwargs: True
    sys.modules["database"] = fake_database


class FeedProcessingTests(unittest.TestCase):
    def tearDown(self):
        app_module = sys.modules.get("app")
        if app_module and getattr(app_module, "scheduler", None):
            scheduler = app_module.scheduler
            if scheduler.running:
                scheduler.shutdown(wait=False)
        sys.modules.pop("app", None)
        sys.modules.pop("database", None)

    def test_historical_feed_entry_after_last_seen_is_not_re_notified(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            database_file = os.path.join(tmpdir, "subscriptions.db")
            create_schema(database_file)
            install_sqlite_database_module(database_file)

            app_module = importlib.import_module("app")
            if app_module.scheduler.running:
                app_module.scheduler.shutdown(wait=False)

            sub_id = self._seed_easytier_subscription(database_file)
            notifications = []

            async def fake_fetch_feed_content(url, sub_id):
                return EASYTIER_FEED, None

            def fake_send_bark_notification(**kwargs):
                notifications.append(kwargs)
                return True, {"messageid": "test"}

            app_module.fetch_feed_content = fake_fetch_feed_content
            app_module.send_bark_notification = fake_send_bark_notification
            app_module.send_mqtt_notification = lambda payload: True

            app_module.process_feed(sub_id, is_test_run=False)

            self.assertEqual([], notifications)
            with sqlite3.connect(database_file) as conn:
                conn.row_factory = sqlite3.Row
                sub = conn.execute(
                    "SELECT last_fetched_item_link FROM subscriptions WHERE id = ?",
                    (sub_id,),
                ).fetchone()
                old_item = conn.execute(
                    "SELECT 1 FROM feed_items WHERE link = ?",
                    ("https://github.com/EasyTier/EasyTier/releases/tag/v2.6.2",),
                ).fetchone()

            self.assertEqual(
                "https://github.com/EasyTier/EasyTier/releases/tag/v2.6.4",
                sub["last_fetched_item_link"],
            )
            self.assertIsNone(old_item)

    def _seed_easytier_subscription(self, database_file):
        conn = sqlite3.connect(database_file)
        try:
            sub_id = conn.execute(
                """
                INSERT INTO subscriptions
                    (name, url, interval_minutes, bark_key, bark_level, last_fetched_item_link, is_active, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "easytier update",
                    "https://github.com/EasyTier/EasyTier/releases.atom",
                    60,
                    "bark-key",
                    "",
                    "https://github.com/EasyTier/EasyTier/releases/tag/v2.6.4",
                    1,
                    datetime.now(timezone.utc).isoformat(),
                ),
            ).lastrowid
            conn.executemany(
                """
                INSERT INTO feed_items (subscription_id, title, link, fetched_at)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        sub_id,
                        "v2.6.4",
                        "https://github.com/EasyTier/EasyTier/releases/tag/v2.6.4",
                        datetime.now(timezone.utc).isoformat(),
                    ),
                    (
                        sub_id,
                        "v2.6.3",
                        "https://github.com/EasyTier/EasyTier/releases/tag/v2.6.3",
                        datetime.now(timezone.utc).isoformat(),
                    ),
                ],
            )
            conn.commit()
            return sub_id
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
