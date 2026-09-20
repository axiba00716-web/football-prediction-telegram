"""main / bot 启动链路测试：同步入口、缺 Token 报错、polling 只被同步调用一次。"""

from __future__ import annotations

import sys

import pytest


@pytest.fixture(autouse=True)
def _prep(monkeypatch, tmp_path):
    for mod in ("app.main", "app.config", "app.db", "app.bot"):
        sys.modules.pop(mod, None)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'main.db'}")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("FOOTBALL_API_KEY", "test-key")
    yield


class FakeApp:
    """替身：记录 handler 数量与 run_polling 调用参数。"""

    def __init__(self):
        self.handlers = []
        self.polling_calls = []

    def add_handler(self, h):
        self.handlers.append(h)

    def run_polling(self, **kwargs):
        self.polling_calls.append(kwargs)


# ---------- 缺 Token → 报错退出 ----------

def test_missing_token_exits(monkeypatch):
    import app.main as main_mod

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    built = {"called": False}

    def fake_build():
        built["called"] = True
        return FakeApp()

    monkeypatch.setattr(main_mod, "build_application", fake_build)

    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        main_mod.main()
    assert not built["called"], "缺 Token 不应到达 build_application"


# ---------- 有 Token → 初始化 DB 并同步阻塞 polling ----------

def test_main_runs_polling_synchronously(monkeypatch):
    import app.main as main_mod

    app = FakeApp()
    order = []

    monkeypatch.setattr(main_mod, "build_application", lambda: (order.append("build"), app)[1])
    monkeypatch.setattr(main_mod, "init_db", lambda: order.append("init_db"))

    main_mod.main()

    assert order == ["init_db", "build"], "应先初始化数据库再构建 Application"
    assert len(app.polling_calls) == 1
    assert app.polling_calls[0].get("drop_pending_updates") is True


def test_main_is_sync_function():
    import inspect

    import app.main as main_mod
    assert not inspect.iscoroutinefunction(main_mod.main), "main() 必须是同步函数"
    src = inspect.getsource(main_mod)
    assert "await application.run_polling" not in src, "禁止 await run_polling()"
    assert "await app.run_polling" not in src, "禁止 await run_polling()"
    assert "asyncio.run(" not in src, "禁止用 asyncio.run 包装轮询"


# ---------- build_application ----------

def test_build_application_registers_all_commands(monkeypatch):
    import app.bot as bot_mod

    app = FakeApp()
    built = bot_mod.build_application(application=app)
    assert built is app
    names = {h.command for h in app.handlers}
    # 与 BOT_COMMANDS 菜单保持完全一致（菜单点得到 = 有处理器）
    assert names == {cmd for cmd, _ in bot_mod.BOT_COMMANDS}
    assert {"select", "binary", "report", "stats"} <= names, \
        "三大口径命令必须已注册"


def test_build_application_requires_token(monkeypatch):
    import app.bot as bot_mod
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        bot_mod.build_application(application=FakeApp())
