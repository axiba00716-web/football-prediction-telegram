"""配置：统一从环境变量 / .env 读取，全部使用大写变量名。

Railway 部署时在 Variables 中填写同名变量即可覆盖默认值。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import ConfigDict
from pydantic_settings import BaseSettings

DEFAULT_BASE_URL = "https://v3.football.api-sports.io"


class Settings(BaseSettings):
    """全部运行时配置；变量名与 Railway Variables 一一对应。"""

    model_config = ConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # Telegram
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_ADMIN_CHAT_ID: str = ""

    # API-Football
    FOOTBALL_API_KEY: str = ""
    FOOTBALL_API_BASE_URL: str = DEFAULT_BASE_URL

    # 数据库
    DATABASE_URL: str = "sqlite:///./football.db"

    # 运行配置
    TIMEZONE: str = "Asia/Shanghai"
    ENABLED_LEAGUES: str = "39,140,135,78,61"
    MIN_HISTORY_MATCHES: int = 5
    PREDICTION_ENABLED: bool = True

    # LLM（预留，留空即可）
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://api.openai.com/v1"
    LLM_MODEL: str = ""

    LOG_LEVEL: str = "INFO"

    @property
    def enabled_league_ids(self) -> list[int]:
        """解析 ENABLED_LEAGUES 字符串为 int 列表，忽略空项与非法项。"""
        if not self.ENABLED_LEAGUES:
            return []
        out: list[int] = []
        for part in str(self.ENABLED_LEAGUES).split(","):
            p = part.strip()
            if p == "":
                continue
            try:
                out.append(int(p))
            except ValueError:
                continue
        return out


@lru_cache()
def get_settings() -> Settings:
    """返回缓存的 Settings 单例（环境变量变更需重启进程）。"""
    return Settings()


# 常见时区的固定偏移回退表：容器缺少 tzdata 时仍能按用户时区算日期
_FALLBACK_OFFSETS = {
    "Asia/Shanghai": 8,
    "Asia/Chongqing": 8,
    "Asia/Hong_Kong": 8,
    "Asia/Macau": 8,
    "Asia/Taipei": 8,
    "Asia/Singapore": 8,
    "Asia/Kuala_Lumpur": 8,
    "Asia/Manila": 8,
    "Asia/Tokyo": 9,
    "Asia/Seoul": 9,
    "Asia/Bangkok": 7,
    "Asia/Jakarta": 7,
    "Asia/Kolkata": 5.5,
    "Asia/Dubai": 4,
    "Europe/London": 0,
    "Europe/Berlin": 1,
    "Europe/Madrid": 1,
    "Europe/Paris": 1,
    "UTC": 0,
}


def get_timezone():
    """按 ``TIMEZONE`` 返回 tzinfo。

    优先 ``zoneinfo``（需要系统 tzdata）；缺失时按内置偏移表回退，
    再不行退到 UTC。**绝不抛异常**，否则机器人会启动失败。
    """
    from datetime import timedelta, timezone

    name = (get_settings().TIMEZONE or "UTC").strip()
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        pass
    offset = _FALLBACK_OFFSETS.get(name)
    if offset is None:
        return timezone.utc
    return timezone(timedelta(hours=offset))
