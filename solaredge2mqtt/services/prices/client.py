"""Async client for the ENTSO-E Transparency Platform day-ahead prices API.

Docs: https://transparency.entsoe.eu/content/static_content/Static%20content/web%20api/Guide.html
Endpoint: https://web-api.tp.entsoe.eu/api  (documentType=A44, day-ahead prices)

The XML response uses a Publication_MarketDocument schema. Multiple TimeSeries
may be returned, each with a Period whose timeInterval covers a contiguous span
at a given resolution (PT60M for hourly). Points within a period are sparse:
only positions where the price changes are listed, so consumers must
carry-forward the last seen value.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable
from xml.etree import ElementTree as ET

from solaredge2mqtt.core.exceptions import InvalidDataException
from solaredge2mqtt.core.logging import logger
from solaredge2mqtt.services.energy.settings import EntsoePriceSourceSettings
from solaredge2mqtt.services.http_async import HTTPClientAsync

ENTSOE_URL = "https://web-api.tp.entsoe.eu/api"
DOCUMENT_TYPE_DAY_AHEAD = "A44"
RESOLUTION_HOURLY = "PT60M"


class EntsoePricesUnavailableError(RuntimeError):
    """Raised when ENTSO-E returns no day-ahead prices for the requested window."""


class EntsoeClient:
    def __init__(self, settings: EntsoePriceSourceSettings) -> None:
        self.settings = settings
        self.http = HTTPClientAsync("entsoe")

    async def close(self) -> None:
        await self.http.close()

    async def fetch_day_ahead(
        self, period_start: datetime, period_end: datetime
    ) -> dict[datetime, float]:
        """Fetch hourly day-ahead spot prices in EUR/MWh, keyed by UTC hour."""
        params: dict[str, str | int | float] = {
            "securityToken": self.settings.api_token,
            "documentType": DOCUMENT_TYPE_DAY_AHEAD,
            "in_Domain": self.settings.area,
            "out_Domain": self.settings.area,
            "periodStart": _format_period(period_start),
            "periodEnd": _format_period(period_end),
        }

        body = await self.http._get(  # noqa: SLF001 — http_async exposes only protected helpers
            ENTSOE_URL, params=params, expect_json=False
        )
        if body is None:
            raise EntsoePricesUnavailableError("ENTSO-E request returned no body")
        if not isinstance(body, str):
            raise EntsoePricesUnavailableError(
                f"Unexpected ENTSO-E response type: {type(body).__name__}"
            )

        return parse_day_ahead_xml(body)


def _format_period(when: datetime) -> str:
    if when.tzinfo is None:
        raise ValueError("ENTSO-E period bounds must be timezone-aware")
    return when.astimezone(timezone.utc).strftime("%Y%m%d%H%M")


def parse_day_ahead_xml(body: str) -> dict[datetime, float]:
    """Parse a Publication_MarketDocument into a UTC-hour → EUR/MWh map.

    Sparse Point lists are expanded by carrying the last price forward across
    positions until the next change.
    """
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise InvalidDataException(f"ENTSO-E XML parse error: {exc}") from exc

    root_tag = _strip_ns(root.tag)
    if root_tag == "Acknowledgement_MarketDocument":
        reason_code = _find_text(root, "code") or "?"
        reason_text = (_find_text(root, "text") or "").strip()
        raise EntsoePricesUnavailableError(
            f"ENTSO-E acknowledgement {reason_code}: {reason_text or 'no detail'}"
        )

    prices: dict[datetime, float] = {}

    for time_series in _iter_local(root, "TimeSeries"):
        for period in _iter_local(time_series, "Period"):
            resolution = _find_text(period, "resolution")
            if resolution != RESOLUTION_HOURLY:
                logger.debug(
                    "Skipping ENTSO-E period with unsupported resolution {res}",
                    res=resolution,
                )
                continue

            interval = _find_first(period, "timeInterval")
            if interval is None:
                continue
            start_text = _find_text(interval, "start")
            if start_text is None:
                continue
            period_start = _parse_iso_utc(start_text)

            last_price: float | None = None
            points = sorted(
                (
                    (
                        int(_find_text(p, "position") or "0"),
                        _find_text(p, "price.amount"),
                    )
                    for p in _iter_local(period, "Point")
                ),
                key=lambda item: item[0],
            )
            if not points:
                continue

            max_position = points[-1][0]
            point_map = {pos: amount for pos, amount in points if amount is not None}

            for position in range(1, max_position + 1):
                if position in point_map:
                    last_price = float(point_map[position])
                if last_price is None:
                    continue
                hour = period_start + timedelta(hours=position - 1)
                prices[hour] = last_price

    if not prices:
        raise EntsoePricesUnavailableError("No hourly prices in ENTSO-E response")

    return prices


def _iter_local(element: ET.Element, tag: str) -> Iterable[ET.Element]:
    for child in element.iter():
        if _strip_ns(child.tag) == tag and child is not element:
            yield child


def _find_first(element: ET.Element, tag: str) -> ET.Element | None:
    for child in element.iter():
        if _strip_ns(child.tag) == tag and child is not element:
            return child
    return None


def _find_text(element: ET.Element, tag: str) -> str | None:
    found = _find_first(element, tag)
    return found.text if found is not None else None


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _parse_iso_utc(value: str) -> datetime:
    cleaned = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(cleaned)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
