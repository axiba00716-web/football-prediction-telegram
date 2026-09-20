from pydantic_settings import BaseSettings
from functools import lru_cache


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

    @property
    def enabled_league_ids(self) -> list[int]:
        return [int(x) for x in self.ENABLED_LEAGUES.split(",") if x.strip()]

    model_config = {"env_file": ".env", "case_sensitive": True, "extra": "ignore"}


@lru_cache()
def get_settings() -> Settings:
    return Settings()
