"""配置：统一从环境变量 / .env 读取，全部使用大写变量名。"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_ADMIN_CHAT_ID: str = ""
    FOOTBALL_API_KEY: str = ""
    FOOTBALL_API_BASE_URL: str = "https://v3.football.api-sports.io"
    DATABASE_URL: str = "sqlite:///./football.db"
    TIMEZONE: str = "Asia/Shanghai"
    ENABLED_LEAGUES: str = "39,140,135,78,61"
    MIN_HISTORY_MATCHES: int = 5
    PREDICTION_ENABLED: bool = True
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://api.openai.com/v1"
    LLM_MODEL: str = ""
    LOG_LEVEL: str = "INFO"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True

    @property
    def enabled_league_ids(self) -> list[int]:
        """解析 ENABLED_LEAGUES 字符串为 int 列表，忽略空项。"""
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
