import importlib
import os
import sys
import tempfile
import types as module_types
import unittest
from contextlib import contextmanager


class DummyCursor:
    lastrowid = 1
    rowcount = 0

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class DummyConnection:
    def execute(self, *args, **kwargs):
        return DummyCursor()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@contextmanager
def dummy_db_connection(*args, **kwargs):
    yield DummyConnection()


def install_fake_database_module(database_file):
    fake_database = module_types.ModuleType("database")
    fake_database.DATABASE_FILE = database_file
    fake_database._db_lock = module_types.SimpleNamespace(
        __enter__=lambda self: None,
        __exit__=lambda self, exc_type, exc, tb: None,
    )
    fake_database.get_db_connection = dummy_db_connection
    fake_database.init_db = lambda: None
    fake_database.cleanup_old_feed_items = lambda *args, **kwargs: 0
    fake_database.get_detailed_feed_items_for_summary = lambda *args, **kwargs: []
    fake_database.get_all_keyword_triggers = lambda: []
    fake_database.add_keyword_trigger = lambda *args, **kwargs: True
    fake_database.delete_keyword_trigger = lambda *args, **kwargs: True
    fake_database.get_mqtt_config = lambda: None
    fake_database.save_mqtt_config = lambda *args, **kwargs: True
    sys.modules["database"] = fake_database


class LazyAiImportTests(unittest.TestCase):
    def tearDown(self):
        app_module = sys.modules.get("app")
        if app_module and getattr(app_module, "scheduler", None):
            scheduler = app_module.scheduler
            if scheduler.running:
                scheduler.shutdown(wait=False)
        sys.modules.pop("app", None)
        sys.modules.pop("database", None)

    def test_importing_app_does_not_import_ai_sdks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            install_fake_database_module(os.path.join(tmpdir, "subscriptions.db"))
            sys.modules.pop("google.genai", None)
            sys.modules.pop("openai", None)

            importlib.import_module("app")

            self.assertNotIn("google.genai", sys.modules)
            self.assertNotIn("openai", sys.modules)


if __name__ == "__main__":
    unittest.main()
