"""入口：Railway / 本地启动 Telegram Bot（polling 模式）。

生产行为
--------
* 无 TELEGRAM_BOT_TOKEN → 立即报错并退出（非零），**绝不**执行一次同步后假装正常。
* 有 Token → 初始化 DB、构建 Application、持续 run_polling()（Railway 服务常驻）。
"""

from __future__ import annotations

import asyncio
import logging
import sys

from app.bot import build_application
from app.config import get_settings
from app.db import init_db


def _configure_logging() -> None:
    settings = get_settings()
    level = getattr(logging, str(settings.LOG_LEVEL).upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


# 模块级 logger（测试可 monkeypatch 替换以捕获消息）
logger = logging.getLogger(__name__)


async def _run() -> None:
    _configure_logging()
    global logger
    logger = logging.getLogger(__name__)

    # 每次启动重新读取（测试/运行时可通过环境变量动态控制）
    settings = get_settings()
    if not settings.TELEGRAM_BOT_TOKEN:
        logger.error("缺少 TELEGRAM_BOT_TOKEN，无法启动 Telegram Bot。")
        raise RuntimeError("缺少 TELEGRAM_BOT_TOKEN，无法启动 Telegram Bot。")

    if not settings.FOOTBALL_API_KEY:
        logger.warning(
            "FOOTBALL_API_KEY 未配置：/today、/tomorrow、/predict 将无法拉取数据，"
            "但机器人仍可启动并响应 /start、/status。"
        )

    init_db()
    logger.info("Initializing database...")

    app = build_application()
    logger.info("Starting Telegram bot (polling)...")
    await app.run_polling()  # 阻塞：服务持续运行


def run_polling() -> None:
    """供规范要求的 ``main`` 调用；内部即 _run 的同步驱动。"""
    asyncio.run(_run())


def main() -> None:
    settings = get_settings()
    if not settings.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("缺少 TELEGRAM_BOT_TOKEN，无法启动 Telegram Bot。")
    init_db()
    run_polling()


if __name__ == "__main__":
    main()
