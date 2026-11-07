import math
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx

from eventradar.domain import Item


class Routes:
    def __init__(self, key: str):
        self.key = key
        self.client = httpx.AsyncClient(timeout=15)

    async def describe(self, profile: dict, item: Item) -> str:
        if not item.venue:
            return ""
        destination = ", ".join(part for part in (item.city, item.venue) if part)
        # Home address is deliberately excluded from shareable map links.
        link = "https://www.google.com/maps/dir/?" + urlencode({"api": 1, "destination": destination, "travelmode": "transit"})
        result = "Маршрут: " + link
        if not profile["routing"] or not profile["address"] or not self.key or not item.starts_at:
            return result
        # Transit providers do not promise schedules far in the future.
        if item.starts_at > datetime.now(UTC) + timedelta(days=7):
            return result + "\nОценка дороги появится ближе к событию (до 7 дней)."
        origin = ", ".join(part for part in (profile["region"], profile["city"], profile["address"]) if part)
        try:
            response = await self.client.post(
                "https://routes.googleapis.com/directions/v2:computeRoutes",
                headers={"X-Goog-Api-Key": self.key, "X-Goog-FieldMask": "routes.duration,routes.legs.steps.transitDetails"},
                json={"origin": {"address": origin}, "destination": {"address": destination}, "travelMode": "TRANSIT",
                      "arrivalTime": item.starts_at.astimezone(UTC).isoformat().replace("+00:00", "Z"), "languageCode": "ru"},
            )
            response.raise_for_status()
            routes = response.json().get("routes", [])
            if not routes:
                return result + "\nРасписание транспорта для маршрута недоступно."
            route = routes[0]
            minutes = math.ceil(float(route["duration"].removesuffix("s")) / 60)
            rides = sum(1 for leg in route.get("legs", []) for step in leg.get("steps", []) if step.get("transitDetails"))
            return result + f"\nДорога ≈ {minutes} мин., пересадок: {max(0, rides - 1)}. По расписанию Google Maps."
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return result + "\nНе удалось оценить дорогу; откройте маршрут для уточнения."
