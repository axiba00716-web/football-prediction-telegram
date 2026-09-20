import logging
import asyncio
from datetime import date, timedelta

from app.config import get_settings
from app.db import init_db
from app.data import sync_date
import app.bot as bot_module

logging.basicConfig(
    level=get_settings().LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def daily_sync():
    """每日同步今天+明天比赛（供 Railway 定时调用）。"""
    today = date.today()
    for d in (today, today + timedelta(days=1)):
        try:
            count, _ = await sync_date(d)
            logger.info("Synced %s: %d fixtures", d, count)
        except Exception as e:
            logger.error("Sync failed for %s: %s", d, e)


def main():
    settings = get_settings()
    logger.info("Initializing database...")
    init_db()

    if not settings.TELEGRAM_BOT_TOKEN:
        # 无 Token 时（如 Railway 构建/测试阶段）仅跑同步，不启动 Bot
        logger.warning("TELEGRAM_BOT_TOKEN 未配置，跳过 Bot 启动，仅执行每日同步。")
        asyncio.run(daily_sync())
        return

    logger.info("Starting Telegram bot (polling)...")
    bot_module.run_polling()


if __name__ == "__main__":
    main()
