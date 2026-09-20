"""入口：Railway / 本地启动 Telegram Bot（polling 模式）。

生产行为
--------
* 无 TELEGRAM_BOT_TOKEN → 立即报错并退出（非零），**绝不**执行一次同步后假装正常。
* 有 Token → 初始化 DB、构建 Application、持续 run_polling()（Railway 服务常驻）。

注意：python-telegram-bot 的 ``run_polling()`` 是**同步阻塞**方法，
本模块不对其 ``await``，也不经 ``asyncio.run`` 包装，避免事件循环冲突。
"""

from __future__ import annotations

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


logger = logging.getLogger(__name__)


def run_polling() -> None:
    """同步阻塞启动 Bot；调用方直接调用，不 await、不包 asyncio.run。"""
    _configure_logging()

    settings = get_settings()
    if not settings.TELEGRAM_BOT_TOKEN:
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
    # 同步阻塞调用：run_polling() 自带事件循环，不应被 await
    app.run_polling()


def main() -> None:
    """Railway / CLI 同步入口。"""
    settings = get_settings()
    if not settings.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("缺少 TELEGRAM_BOT_TOKEN，无法启动 Telegram Bot。")
    init_db()
    run_polling()


if __name__ == "__main__":
    main()
