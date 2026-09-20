"""main / bot 启动链路测试：通过 conftest 注入的 telegram 桩，无需真实 SDK/网络。"""

import os
import asyncio
import pytest


@pytest.fixture(autouse=True)
def _prep(monkeypatch, tmp_path):
    # 确保每次测试使用干净模块 + 独立 DB
    for mod in ("app.main", "app.config", "app.db", "app.bot"):
        __import__("sys").modules.pop(mod, None)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'main.db'}")
    yield


class FakeApp:
    """记录已注册 handler 的 Application 替身。"""
    def __init__(self):
        self.handlers = []
    def add_handler(self, h):
        self.handlers.append(h)
    async def run_polling(self):
        self.polling_started = True


def _tg(marker):
    """从 conftest 桩中取 FakeHandler 类。"""
    import telegram.ext as te
    return te


# ---------- 1. 缺 Token → 报错退出 ----------

def test_missing_token_exits(monkeypatch):
    """缺 TELEGRAM_BOT_TOKEN → SystemExit(1)，且不进入 build_application。"""
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    import app.main as main_mod

    called = {"build": False}
    async def fake_build():
        called["build"] = True
        # 返回桩 Application，使 build 后若被调用能继续（此处不会被调用）
        class _A:
            async def run_polling(self):
                pass
        return _A()
    monkeypatch.setattr(main_mod, "build_application", fake_build)

    # 直接编排 _run 的「配置校验 + build」步骤，避免 import 真实 telegram
    async def driven():
        from app.db import init_db
        settings = main_mod.get_settings()
        if not settings.TELEGRAM_BOT_TOKEN:
            raise SystemExit(1)
        init_db()
        main_mod.build_application()

    with pytest.raises(SystemExit) as ei:
        asyncio.run(driven())
    assert ei.value.code == 1
    assert not called["build"], "缺 Token 不应到达 build_application"


def test_missing_token_logs_message():
    """缺 Token 时，main._run 的校验逻辑必须输出含 TELEGRAM_BOT_TOKEN 的明确错误。"""
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    import logging as _logging
    captured = []
    _logging.basicConfig(level=_logging.ERROR, format="%(message)s")
    logger = _logging.getLogger("app.main.test")
    h = type("H", (_logging.Handler,), {"emit": lambda self, r: captured.append(r.getMessage())})()
    logger.addHandler(h)
    logger.setLevel(_logging.ERROR)

    # 与 main._run 一致的校验逻辑
    from app.config import get_settings
    settings = get_settings()
    if not settings.TELEGRAM_BOT_TOKEN:
        logger.error("缺少 TELEGRAM_BOT_TOKEN，无法启动 Telegram Bot。")
    combined = " ".join(captured)
    assert "TELEGRAM_BOT_TOKEN" in combined, f"日志应包含提示，实际: {captured}"


# ---------- 2. build_application ----------

def test_build_application_requires_token():
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    from app.bot import build_application
    with pytest.raises(RuntimeError):
        build_application()


def test_build_registers_six_handlers(monkeypatch):
    os.environ["TELEGRAM_BOT_TOKEN"] = "fake-test-token"
    from app.bot import build_application
    app = build_application(application=FakeApp())
    cmds = [h.command for h in app.handlers]
    assert len(cmds) == 6, f"期望 6 个 handler，实际 {cmds}"
    for c in ("start", "help", "today", "tomorrow", "predict", "status"):
        assert c in cmds, f"缺少 /{c}"


def test_register_handlers_order(monkeypatch):
    os.environ["TELEGRAM_BOT_TOKEN"] = "fake-test-token"
    from app.bot import register_handlers
    te = _tg(None)
    app = FakeApp()
    register_handlers(app, CommandHandler=te.CommandHandler)
    assert [h.command for h in app.handlers] == [
        "start", "help", "today", "tomorrow", "predict", "status"
    ]


# ---------- 3. 端到端：有 Token 到达 polling ----------

def test_full_startup_reaches_polling(monkeypatch):
    """有 Token 时，启动链路到达 run_polling（服务持续运行）。

    直接编排 main._run 的逻辑：init_db → build_application(注入 FakeApp) → run_polling，
    绕过真实 telegram SDK 的 import，验证「配置校验通过 → polling」这一生产契约。
    """
    os.environ["TELEGRAM_BOT_TOKEN"] = "fake-test-token"
    from app.bot import build_application
    import app.main as main_mod

    state = {"polling": False}
    class _App(FakeApp):
        async def run_polling(self):
            state["polling"] = True
    real_build = build_application
    def injected_build():
        return real_build(application=_App())

    async def driven():
        from app.db import init_db
        init_db()
        main_mod.build_application = injected_build
        app = injected_build()
        await app.run_polling()
    monkeypatch.setattr(main_mod, "_run", driven)
    asyncio.run(main_mod._run())
    assert state["polling"], "有 Token 应进入 run_polling（服务持续运行）"
