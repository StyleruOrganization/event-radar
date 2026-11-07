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
    owner_telegram_id: int = Field(gt=0)
    telegram_api_id: int = Field(gt=0)
    telegram_api_hash: SecretStr
    telegram_reader_session: SecretStr
    chatgpt_auth_dir: Path = Path("/data/chatgpt")
    chatgpt_model: str = ""
    default_timezone: str = "Europe/Moscow"
    source_timezone: str = "Europe/Moscow"
    database_path: Path = Path("/data/eventradar.sqlite3")
    initial_lookback_days: int = Field(default=7, ge=1, le=30)
    max_channels_per_user: int = Field(default=30, ge=1, le=100)
    ai_batch_size: int = Field(default=10, ge=1, le=20)
    ai_batch_max_characters: int = Field(default=30000, ge=24000, le=100000)
    max_ai_calls_per_day: int = Field(default=300, ge=1)
    max_photos_per_report: int = Field(default=3, ge=0, le=20)

    @field_validator("default_timezone", "source_timezone")
    @classmethod
    def timezone_exists(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @field_validator("telegram_bot_token", "telegram_api_hash", "telegram_reader_session")
    @classmethod
    def require_secret(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("Required secret is empty")
        return value

    @property
    def users(self) -> frozenset[int]:
        return frozenset({self.owner_telegram_id})
