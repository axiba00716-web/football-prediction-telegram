"""入口：Railway / 本地启动 Telegram Bot（polling 模式，同步入口）。

生产行为
--------
* 无 TELEGRAM_BOT_TOKEN → 立即抛错退出（非零），**绝不**执行一次同步后假装正常。
* 有 Token → 初始化数据库 → 构建 Application → 持续 ``run_polling()``（常驻服务）。

注意：python-telegram-bot 的 ``run_polling()`` 是**同步阻塞**方法，
本模块不对其 ``await``，也不用 ``asyncio.run`` 包装，避免事件循环冲突。
"""

from __future__ import annotations

import logging
import sys

from app.bot import build_application
from app.config import get_settings
from app.db import init_db

logger = logging.getLogger(__name__)


def _configure_logging(level_name: str) -> None:
    level = getattr(logging, str(level_name).upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


def main() -> None:
    """同步入口：检查环境变量 → 初始化数据库 → 启动 polling。"""
    settings = get_settings()
    _configure_logging(settings.LOG_LEVEL)

    if not settings.TELEGRAM_BOT_TOKEN:
        logger.error("缺少 TELEGRAM_BOT_TOKEN，无法启动 Telegram Bot。")
        raise RuntimeError(
            "缺少 TELEGRAM_BOT_TOKEN，无法启动 Telegram Bot。"
        )

    init_db()
    logger.info("Database initialized.")

    # 启动探测一次免费套餐能力（赔率/积分榜是否可用），结果直接打进日志。
    # 仅 5 次请求且同次部署不重复；失败绝不阻断启动。
    try:
        from app.probe import probe_once_on_start
        probe_once_on_start()
    except Exception as e:  # noqa: BLE001
        logger.warning("启动探测跳过: %s", e)

    if not settings.FOOTBALL_API_KEY:
        logger.warning(
            "FOOTBALL_API_KEY 未配置，/today、/tomorrow、/predict 可能无法同步数据。"
        )

    application = build_application()
    logger.info("Starting Telegram bot in polling mode")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
