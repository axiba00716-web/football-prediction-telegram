"""测试公共 fixture：数据库隔离 + 临时目录 + telegram 桩。"""

from __future__ import annotations

import sys
import types
import pytest


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """每个测试独立 DB + 注入 telegram 桩，重置模块级单例。"""
    # 必须先卸载 app.bot（它可能已缓存真实 telegram 引用），再装桩
    for mod in ("app.bot", "app.main", "app.config", "app.db",
                "telegram", "telegram.ext"):
        sys.modules.pop(mod, None)

    # 注入 telegram 桩（覆盖 bot.py 运行时的 `from telegram.ext import ...`）
    tg = types.ModuleType("telegram")
    te = types.ModuleType("telegram.ext")

    class _FakeHandler:
        def __init__(self, command, func):
            self.command = command
            self.func = func
    te.CommandHandler = _FakeHandler
    te.ContextTypes = object
    te.Application = type("A", (), {
        "builder": lambda: type("B", (), {
            "token": lambda self, t: self,
            "build": lambda self: type("FakeApp", (), {
                "__init__": lambda self: setattr(self, "handlers", []),
                "add_handler": lambda self, h: self.handlers.append(h),
            })(),
        })()
    })
    tg.Update = object
    tg.ContextTypes = object

    class BotCommand:
        def __init__(self, command, description):
            self.command = command
            self.description = description
    tg.BotCommand = BotCommand
    sys.modules["telegram"] = tg
    sys.modules["telegram.ext"] = te

    import app.db as db_module
    db_module.reset_db_state()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("MIN_HISTORY_MATCHES", "5")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token-for-db")
    monkeypatch.setenv("FOOTBALL_API_KEY", "test-key")
    yield


@pytest.fixture()
def fake_telegram():
    """暴露桩类，便于测试构造注入对象。"""
    import telegram.ext as te
    yield te
