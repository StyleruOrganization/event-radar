import hashlib
import json
import re
from datetime import datetime, time, timedelta
from typing import Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator


class Item(BaseModel):
    kind: Literal["event", "post"]
    title: str = Field(max_length=180)
    summary: str = Field(max_length=1000)
    starts_at: datetime | None
    ends_at: datetime | None
    city: str | None = Field(max_length=120)
    venue: str | None = Field(max_length=300)
    registration_url: str | None = Field(max_length=2048)
    cancelled: bool

    @model_validator(mode="after")
    def sensible_duration(self):
        if self.starts_at and self.ends_at:
            duration = self.ends_at - self.starts_at
            if duration <= timedelta(0) or duration > timedelta(days=14):
                self.ends_at = None
        return self

    @field_validator("starts_at", "ends_at")
    @classmethod
    def aware_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("Event dates must include timezone")
        return value

    @field_validator("registration_url")
    @classmethod
    def safe_link(cls, value: str | None) -> str | None:
        if value is not None:
            parsed = urlparse(value)
            if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError("Invalid registration URL")
        return value


class Extraction(BaseModel):
    items: list[Item] = Field(max_length=12)


class Decision(BaseModel):
    relevant: bool
    reason: str = Field(max_length=450)


class Recommendation(BaseModel):
    item: Item
    reason: str = Field(max_length=450)


class AnalyzedPost(BaseModel):
    post_id: int
    recommendations: list[Recommendation] = Field(max_length=12)


class BatchAnalysis(BaseModel):
    publications: list[AnalyzedPost] = Field(max_length=20)


class TravelEstimate(BaseModel):
    min_minutes: int | None = Field(ge=1, le=1440)
    max_minutes: int | None = Field(ge=1, le=1440)
    min_transfers: int | None = Field(ge=0, le=12)
    max_transfers: int | None = Field(ge=0, le=12)
    explanation: str = Field(min_length=1, max_length=350)

    @model_validator(mode="after")
    def valid_ranges(self) -> "TravelEstimate":
        for minimum, maximum in ((self.min_minutes, self.max_minutes), (self.min_transfers, self.max_transfers)):
            if (minimum is None) != (maximum is None):
                raise ValueError("Estimate range must have both bounds or neither")
            if minimum is not None and maximum is not None and minimum > maximum:
                raise ValueError("Estimate range is reversed")
        if self.min_minutes is None and self.min_transfers is not None:
            raise ValueError("Transfers require an estimated journey")
        return self


def fingerprint(*values: object) -> str:
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def channel_username(raw: str) -> str:
    value = raw.strip()
    if value.startswith(("https://t.me/", "http://t.me/", "t.me/")):
        value = value.split("t.me/", 1)[1].rstrip("/")
    value = value.removeprefix("@")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", value):
        raise ValueError("Укажите @username публичного канала или https://t.me/username без ссылки на пост.")
    return value.lower()


def clock(value: str) -> str:
    if not re.fullmatch(r"\d{2}:\d{2}", value):
        raise ValueError("Время должно быть в формате ЧЧ:ММ.")
    time.fromisoformat(value)
    return value


def overlaps(item: Item, rules: list[dict], timezone: str) -> bool:
    if item.starts_at is None or not rules:
        return False
    start = item.starts_at.astimezone(ZoneInfo(timezone))
    end = (item.ends_at or item.starts_at + timedelta(hours=2)).astimezone(ZoneInfo(timezone))
    if end <= start:
        end = start + timedelta(hours=2)
    # Include previous day for rules spanning midnight.
    day = start.date() - timedelta(days=1)
    while day <= end.date():
        for rule in rules:
            first, last = rule["from_month"], rule["to_month"]
            in_season = first <= day.month <= last if first <= last else day.month >= first or day.month <= last
            if not in_season or day.isoweekday() != rule["weekday"]:
                continue
            blocked_start = datetime.combine(day, time.fromisoformat(rule["start_time"]), ZoneInfo(timezone))
            blocked_end = datetime.combine(day, time.fromisoformat(rule["end_time"]), ZoneInfo(timezone))
            if blocked_end <= blocked_start:
                blocked_end += timedelta(days=1)
            if start < blocked_end and end > blocked_start:
                return True
        day += timedelta(days=1)
    return False
