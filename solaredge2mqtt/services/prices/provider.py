"""Dynamic-price provider.

Pulls hourly day-ahead prices from ENTSO-E once per day and exposes the
current-hour ``price_in`` / ``price_out`` to the rest of the service.

When ``prices.source.type == "static"`` (the default) this provider is a
no-op wrapper around the configured static fallback values, so the existing
behavior is preserved.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING

from influxdb_client.client.write.point import Point
from tzlocal import get_localzone

from solaredge2mqtt.core.events import EventBus
from solaredge2mqtt.core.logging import logger
from solaredge2mqtt.core.timer.events import (
    Interval15MinTriggerEvent,
    IntervalBaseTriggerEvent,
)
from solaredge2mqtt.services.energy.settings import (
    PriceSettings,
    PriceSourceType,
)
from solaredge2mqtt.services.prices.client import (
    EntsoeClient,
    EntsoePricesUnavailableError,
)
from solaredge2mqtt.services.prices.events import PricesUpdatedEvent

if TYPE_CHECKING:
    from solaredge2mqtt.core.influxdb import InfluxDBAsync


LOCAL_TZ = get_localzone()


class PriceView:
    """Plug-compatible stand-in for :class:`PriceSettings` exposing
    ``price_in`` / ``price_out`` for the current hour."""

    def __init__(self, price_in: float, price_out: float) -> None:
        self.price_in = price_in
        self.price_out = price_out


class PriceProvider:
    def __init__(
        self,
        settings: PriceSettings,
        event_bus: EventBus,
        influxdb: "InfluxDBAsync | None" = None,
    ) -> None:
        self.settings = settings
        self.event_bus = event_bus
        self.influxdb = influxdb

        self._cache: dict[datetime, tuple[float, float]] = {}
        self._last_fetch_date: date | None = None
        self._initialized = False

        self._client: EntsoeClient | None = None
        if self.settings.source.type == PriceSourceType.ENTSOE:
            assert self.settings.source.entsoe is not None
            self._client = EntsoeClient(self.settings.source.entsoe)

        self._subscribe_events()

    def _subscribe_events(self) -> None:
        if not self.is_dynamic:
            return
        self.event_bus.subscribe(IntervalBaseTriggerEvent, self._initial_fetch)
        self.event_bus.subscribe(Interval15MinTriggerEvent, self._maybe_daily_fetch)

    @property
    def is_dynamic(self) -> bool:
        return self.settings.is_dynamic and self._client is not None

    async def close(self) -> None:
        if self._client:
            await self._client.close()

    def current(self, when: datetime | None = None) -> PriceView:
        """Return the price view for ``when`` (UTC hour), with static fallback."""
        fallback = PriceView(self.settings.price_in, self.settings.price_out)
        if not self.is_dynamic:
            return fallback

        moment = (when or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
        hour = moment.replace(minute=0, second=0, microsecond=0)
        cached = self._cache.get(hour)
        if cached is None:
            return fallback
        return PriceView(cached[0], cached[1])

    async def _initial_fetch(self, event: IntervalBaseTriggerEvent) -> None:
        del event
        if self._initialized:
            return
        self._initialized = True
        today = _today_local()

        source = self.settings.source.entsoe
        backfill = source.backfill_days if source else 0

        first_day = today - timedelta(days=backfill)
        last_day = today + timedelta(days=1)  # also pull tomorrow if available
        days = [
            first_day + timedelta(days=i)
            for i in range((last_day - first_day).days + 1)
        ]

        any_success = await self._fetch_days(days)

        if backfill > 0 and any_success and self.influxdb is not None:
            # Recompute money_* for the full backfill window so existing
            # energy points pick up the freshly-ingested per-hour prices.
            hours = (backfill + 1) * 24
            try:
                await self.influxdb.recompute_money_window(
                    hours=hours,
                    price_in_default=self.settings.price_in,
                    price_out_default=self.settings.price_out,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Money-field recompute over {hours}h failed: {exc}",
                    hours=hours,
                    exc=exc,
                )
            else:
                logger.info(
                    "Recomputed money_* fields over the past {hours}h",
                    hours=hours,
                )

    async def _maybe_daily_fetch(self, event: Interval15MinTriggerEvent) -> None:
        del event
        now_local = datetime.now(tz=LOCAL_TZ)
        source = self.settings.source.entsoe
        if source is None:
            return
        if now_local.hour < source.fetch_hour:
            return
        today = now_local.date()
        if self._last_fetch_date == today:
            return
        if await self._fetch_days([today, today + timedelta(days=1)]):
            self._last_fetch_date = today

    async def _fetch_days(self, days: list[date]) -> bool:
        """Fetch each day independently. Returns True if any day succeeded.

        Day-ahead prices for tomorrow are typically published around 12:45 CET,
        so before that ENTSO-E will return an Acknowledgement document for any
        window that includes tomorrow. Fetching one day at a time lets today's
        prices land even when tomorrow's are still unavailable.
        """
        assert self._client is not None
        any_success = False
        for day in days:
            period_start = _local_midnight_utc(day)
            period_end = _local_midnight_utc(day + timedelta(days=1))
            try:
                spot = await self._client.fetch_day_ahead(period_start, period_end)
            except EntsoePricesUnavailableError as exc:
                logger.info(
                    "ENTSO-E has no day-ahead prices yet for {day}: {exc}",
                    day=day,
                    exc=exc,
                )
                continue
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "ENTSO-E day-ahead fetch failed for {day}: {exc}",
                    day=day,
                    exc=exc,
                )
                continue

            ingested = self._ingest_spot(spot)
            if self.influxdb is not None and ingested:
                await self._write_points(ingested)
            await self.event_bus.emit(
                PricesUpdatedEvent(datetime.now(tz=timezone.utc), len(ingested))
            )
            logger.info(
                "ENTSO-E prices ingested for {day}: {count} hourly slots",
                day=day,
                count=len(ingested),
            )
            any_success = True

        return any_success

    def _ingest_spot(
        self, spot_eur_per_mwh: dict[datetime, float]
    ) -> dict[datetime, tuple[float, float, float]]:
        """Apply markup/tax/VAT and store EUR/kWh into the cache."""
        source = self.settings.source.entsoe
        assert source is not None

        ingested: dict[datetime, tuple[float, float, float]] = {}
        for hour, eur_per_mwh in spot_eur_per_mwh.items():
            spot_kwh = eur_per_mwh / 1000.0
            price_in = (spot_kwh + source.energy_tax_in + source.markup_in) * (
                1.0 + source.vat_in
            )
            price_out = (spot_kwh + source.energy_tax_out + source.markup_out) * (
                1.0 + source.vat_out
            )
            self._cache[hour] = (price_in, price_out)
            ingested[hour] = (spot_kwh, price_in, price_out)
        return ingested

    async def _write_points(
        self, ingested: dict[datetime, tuple[float, float, float]]
    ) -> None:
        assert self.influxdb is not None
        points: list[Point] = []
        for hour, (spot, price_in, price_out) in ingested.items():
            point = Point("prices")
            point.field("spot", spot)
            point.field("price_in", price_in)
            point.field("price_out", price_out)
            point.time(hour)
            points.append(point)
        await self.influxdb.write_points(points)


def _today_local() -> date:
    return datetime.now(tz=LOCAL_TZ).date()


def _local_midnight_utc(when: date) -> datetime:
    local_midnight = datetime.combine(when, time.min, tzinfo=LOCAL_TZ)
    return local_midnight.astimezone(timezone.utc)
