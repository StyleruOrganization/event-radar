import logging
import time
from urllib.parse import urlencode

from eventradar.ai import DailyBudgetExceeded, Intelligence
from eventradar.db import Database
from eventradar.domain import Item, TravelEstimate, fingerprint

logger = logging.getLogger(__name__)


class Routes:
    """Broad AI estimates, without map APIs or live transport schedules."""

    def __init__(self, ai: Intelligence, db: Database):
        self.ai = ai
        self.db = db

    async def describe(self, profile: dict, item: Item) -> str:
        if not item.venue:
            return ""
        destination = ", ".join(part for part in (item.city, item.venue) if part)
        # Home address is deliberately excluded from shareable map links.
        link = "https://www.google.com/maps/dir/?" + urlencode({"api": 1, "destination": destination, "travelmode": "transit"})
        result = "Карта места: " + link
        if not profile["routing"] or not profile["address"] or item.kind != "event" or item.cancelled:
            return result
        origin = ", ".join(part for part in (profile["region"], profile["city"], profile["address"]) if part)
        arrival_at = item.starts_at.isoformat() if item.starts_at else None
        key = fingerprint("travel-v1", origin, destination, arrival_at, self.ai.settings.openai_model)
        try:
            cached = await self.db.one(
                "SELECT payload FROM travel_estimates WHERE user_id=? AND fingerprint=? AND expires_at>?",
                (profile["id"], key, time.time()),
            )
            estimate = TravelEstimate.model_validate_json(cached["payload"]) if cached else await self.ai.estimate_travel(origin, destination, arrival_at)
            if not cached:
                await self.db.execute(
                    "INSERT INTO travel_estimates(user_id,fingerprint,payload,expires_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(user_id,fingerprint) DO UPDATE SET payload=excluded.payload,expires_at=excluded.expires_at",
                    (profile["id"], key, estimate.model_dump_json(), time.time() + 7 * 86400),
                )
                await self.db.execute("DELETE FROM travel_estimates WHERE expires_at<=?", (time.time(),))
        except DailyBudgetExceeded:
            return result + "\nОценка дороги недоступна: достигнут дневной лимит AI."
        except Exception as error:
            logger.warning("Travel estimation unavailable (%s)", type(error).__name__)
            return result + "\nНе удалось получить приблизительную оценку дороги."
        if estimate.min_minutes is None:
            return result + "\nAI не смог оценить дорогу по указанным адресам. " + estimate.explanation
        parts = [result, f"Дорога на общественном транспорте: ориентировочно {estimate.min_minutes}–{estimate.max_minutes} мин."]
        if estimate.min_transfers is not None:
            parts.append(f"Пересадки: ориентировочно {estimate.min_transfers}–{estimate.max_transfers}.")
        parts.extend([estimate.explanation, "Приблизительная оценка AI без карт, пробок и расписания транспорта; перед поездкой уточните маршрут."])
        return "\n".join(parts)
