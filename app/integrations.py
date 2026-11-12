import asyncio
import math
from typing import Any

import httpx


class LocalServiceUnavailable(RuntimeError):
    pass


class LocalServices:
    def __init__(self, settings: dict[str, Any]):
        self.settings = settings
        self.client: httpx.AsyncClient | None = None
        self.travel_cache: dict[tuple[float, float, float, float], float] = {}

    async def start(self):
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(2.0, connect=0.7))

    async def close(self):
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    async def _request(self, method: str, url: str, *, params=None, payload=None, attempts: int = 3):
        for attempt in range(attempts):
            try:
                if self.client is None:
                    async with httpx.AsyncClient(timeout=httpx.Timeout(2.0, connect=0.7)) as client:
                        response = await client.request(method, url, params=params, json=payload)
                else:
                    response = await self.client.request(method, url, params=params, json=payload)
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                if response.status_code not in (429, 503) or attempt + 1 == attempts:
                    return response.status_code, body, response.headers
                try:
                    delay = float(response.headers.get("Retry-After", "1"))
                except ValueError:
                    delay = 1.0
                await asyncio.sleep(min(max(delay, 0.1), 5.0))
            except (httpx.HTTPError, OSError) as exc:
                if attempt + 1 == attempts:
                    return 0, {"error": str(exc)}, {}
                await asyncio.sleep(0.1 * (attempt + 1))
        return 0, {}, {}

    async def clock(self) -> float | None:
        status, body, _ = await self._request("GET", f"{self.settings['GATEWAY_URL']}/clock", attempts=1)
        if status != 200:
            return None
        try:
            return float(body["sim_time"])
        except (KeyError, TypeError, ValueError):
            return None

    async def inventory(self) -> list[dict[str, Any]]:
        status, body, _ = await self._request("GET", f"{self.settings['INVENTORY_URL']}/inventory", attempts=2)
        if status != 200:
            raise LocalServiceUnavailable(f"Inventory service returned HTTP {status}")
        if not isinstance(body, dict) or not isinstance(body.get("items"), list):
            raise LocalServiceUnavailable("Inventory service returned an invalid response")
        return body["items"]

    async def dispatch(self, payload: dict[str, Any]):
        return await self._request(
            "POST",
            f"{self.settings['INVENTORY_URL']}/inventory/dispatch",
            payload=payload,
        )

    async def message(self, payload: dict[str, Any]):
        return await self._request(
            "POST",
            f"{self.settings['GATEWAY_URL']}/messages",
            payload=payload,
        )

    async def gateway_healthy(self) -> bool:
        status, _, _ = await self._request("GET", f"{self.settings['GATEWAY_URL']}/health", attempts=1)
        if status != 200:
            raise LocalServiceUnavailable(f"Gateway health check returned HTTP {status}")
        return True

    async def travel_minutes(self, origin: tuple[float, float], destination: tuple[float, float]) -> float | None:
        key = (*origin, *destination)
        if key in self.travel_cache:
            return self.travel_cache[key]
        status, body, _ = await self._request(
            "GET",
            f"{self.settings['TRAVEL_URL']}/travel",
            params={"from": f"{origin[0]},{origin[1]}", "to": f"{destination[0]},{destination[1]}"},
            attempts=2,
        )
        if status != 200:
            return None
        try:
            minutes = float(body["minutes"])
        except (KeyError, TypeError, ValueError):
            return None
        if not math.isfinite(minutes) or minutes < 0:
            return None
        self.travel_cache[key] = minutes
        return minutes