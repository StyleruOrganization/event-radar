from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_PROMPT = (
    "Выбирай очные бизнес-события, встречи с предпринимателями и полезные лекции. "
    "Требуются конкретные дата, время, место проведения и ссылка регистрации. "
    "Отсеивай рекламу, итоги уже прошедших встреч, онлайн-события и анонсы без этих данных. "
    "Кратко объясняй, чем мероприятие полезно."
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    telegram_bot_token: SecretStr
    allowed_user_ids: str
    telegram_api_id: int = Field(gt=0)
    telegram_api_hash: SecretStr
    telegram_reader_session: SecretStr
    openai_api_key: SecretStr
    openai_model: str = "gpt-4.1-mini"
    default_timezone: str = "Europe/Moscow"
    source_timezone: str = "Europe/Moscow"
    database_path: Path = Path("/data/eventradar.sqlite3")
    poll_interval_seconds: int = Field(default=300, ge=60)
    initial_lookback_days: int = Field(default=7, ge=1, le=30)
    max_channels_per_user: int = Field(default=30, ge=1, le=100)
    max_ai_calls_per_day: int = Field(default=300, ge=1)
    max_posts_per_cycle: int = Field(default=100, ge=1, le=1000)
    max_digest_items: int = Field(default=20, ge=1, le=50)

    @field_validator("default_timezone", "source_timezone")
    @classmethod
    def timezone_exists(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @field_validator("allowed_user_ids")
    @classmethod
    def validate_users(cls, value: str) -> str:
        if not value.strip() or any(int(item.strip()) <= 0 for item in value.split(",")):
            raise ValueError("ALLOWED_USER_IDS must contain positive numeric Telegram user IDs")
        return value

    @field_validator("telegram_bot_token", "telegram_api_hash", "telegram_reader_session", "openai_api_key")
    @classmethod
    def require_secret(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("Required secret is empty")
        return value

    @property
    def users(self) -> frozenset[int]:
        return frozenset(int(item.strip()) for item in self.allowed_user_ids.split(","))
