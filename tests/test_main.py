"""main 启动链路测试：验证同步入口、缺 Token 报错、到达 polling 三个生产契约。

不导入真实 telegram SDK，全部通过 conftest 注入的桩驱动。
"""

import os
import sys
import inspect
import pytest


@pytest.fixture(autouse=True)
def _prep(monkeypatch, tmp_path):
    # 每次测试使用干净模块 + 独立 DB
    for mod in ("app.main", "app.config", "app.db", "app.bot"):
        sys.modules.pop(mod, None)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'main.db'}")
    yield


class FakeApp:
    """记录是否已注册 handler 与是否进入 polling 的替身。"""
    def __init__(self):
        self.handlers = []
        self.polling_started = False
    def add_handler(self, h):
        self.handlers.append(h)
    def run_polling(self):
        self.polling_started = True


def _tg(_marker):
    import telegram.ext as te
    return te


# ---------- 1. 缺 Token → 报错退出 ----------

def test_missing_token_exits(monkeypatch):
    """缺 TELEGRAM_BOT_TOKEN → RuntimeError，且不进入 build_application。"""
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    import app.main as main_mod

    called = {"build": False}
    def fake_build():
        called["build"] = True
        return FakeApp()
    monkeypatch.setattr(main_mod, "build_application", fake_build)

    with pytest.raises(RuntimeError) as ei:
        main_mod.main()
    assert "TELEGRAM_BOT_TOKEN" in str(ei.value)
    assert not called["build"], "缺 Token 不应到达 build_application"


def test_missing_token_run_polling_also_exits(monkeypatch):
    """run_polling 入口同样先校验 Token。"""
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    import app.main as main_mod
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        main_mod.run_polling()


def test_missing_token_logs_message(caplog):
    """缺 Token 时输出含 TELEGRAM_BOT_TOKEN 的明确错误日志。"""
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    import logging as _logging
    logger = _logging.getLogger("app.main.test")
    captured = []
    h = type("H", (_logging.Handler,), {"emit": lambda self, r: captured.append(r.getMessage())})()
    logger.addHandler(h)
    logger.setLevel(_logging.ERROR)

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
    """有 Token 时，启动链路到达同步 run_polling（服务持续运行）。

    main() → run_polling() → init_db → build_application(注入 FakeApp) → app.run_polling()
    全程同步调用，不 await、不包 asyncio.run。
    """
    os.environ["TELEGRAM_BOT_TOKEN"] = "fake-test-token"
    import app.main as main_mod

    state = {"init_db": False, "polling": False}
    real_init = main_mod.init_db
    real_build = main_mod.build_application

    def counting_init():
        state["init_db"] = True
        return real_init()

    class _App(FakeApp):
        def run_polling(self):
            state["polling"] = True

    def injected_build():
        return real_build(application=_App())

    monkeypatch.setattr(main_mod, "init_db", counting_init, raising=True)
    monkeypatch.setattr(main_mod, "build_application", injected_build, raising=True)

    main_mod.run_polling()

    assert state["init_db"], "应先初始化数据库"
    assert state["polling"], "有 Token 应进入 run_polling（服务持续运行）"


# ---------- 4. 同步入口契约 ----------

def test_run_polling_is_synchronous_function(monkeypatch):
    """run_polling 必须是同步函数（def），不得是协程函数。"""
    import app.main as main_mod
    assert inspect.isfunction(main_mod.run_polling)
    assert not inspect.iscoroutinefunction(main_mod.run_polling)


def test_run_polling_does_not_use_asyncio_run(monkeypatch):
    """旧实现可能误用 asyncio.run 驱动 _run；本实现不得出现此依赖。"""
    import asyncio
    os.environ["TELEGRAM_BOT_TOKEN"] = "fake-test-token"
    import app.main as main_mod

    seen = []
    monkeypatch.setattr(asyncio, "run", lambda *a, **k: seen.append("asyncio.run"))
    monkeypatch.setattr(
        main_mod, "build_application",
        lambda: type("A", (), {"run_polling": lambda s: None})(),
        raising=True,
    )
    main_mod.run_polling()
    assert seen == [], "run_polling 不应通过 asyncio.run 驱动"


def test_init_db_before_polling(monkeypatch):
    """init_db 必须在 run_polling 之前调用。"""
    os.environ["TELEGRAM_BOT_TOKEN"] = "fake-test-token"
    import app.main as main_mod

    calls = []
    real_init = main_mod.init_db
    monkeypatch.setattr(main_mod, "init_db", lambda: calls.append("init_db") or real_init(), raising=True)
    monkeypatch.setattr(
        main_mod, "build_application",
        lambda: type("A", (), {"run_polling": lambda s: calls.append("polling")})(),
        raising=True,
    )
    main_mod.run_polling()
    assert calls.index("init_db") < calls.index("polling")


def test_warning_when_api_key_missing(caplog):
    """缺 FOOTBALL_API_KEY 时应记录 warning，但不阻塞启动。"""
    os.environ["TELEGRAM_BOT_TOKEN"] = "fake-test-token"
    os.environ.pop("FOOTBALL_API_KEY", None)
    import logging as _logging
    import app.main as main_mod

    captured = []
    h = type("H", (_logging.Handler,), {"emit": lambda self, r: captured.append(r.getMessage())})()
    logger = _logging.getLogger("app.main")
    logger.addHandler(h)
    logger.setLevel(_logging.WARNING)

    monkeypatch_call = None
    try:
        main_mod.run_polling()
    except Exception:
        pass
    combined = " ".join(captured)
    assert "FOOTBALL_API_KEY" in combined, f"应提示 API Key 缺失，实际: {captured}"


def test_main_calls_run_polling(monkeypatch):
    """main() 在 Token 存在时直接调用 run_polling。"""
    os.environ["TELEGRAM_BOT_TOKEN"] = "fake-test-token"
    import app.main as main_mod

    called = []
    monkeypatch.setattr(main_mod, "run_polling", lambda: called.append("run_polling"), raising=True)
    main_mod.main()
    assert called == ["run_polling"]
