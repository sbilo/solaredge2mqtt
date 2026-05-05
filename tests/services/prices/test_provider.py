"""Tests for PriceProvider."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from solaredge2mqtt.services.energy.settings import (
    PriceSettings,
)
from solaredge2mqtt.services.prices.provider import PriceProvider

ENTSOE_DYNAMIC = {
    "consumption": 0.30,
    "delivery": 0.08,
    "currency": "EUR",
    "source": {
        "type": "entsoe",
        "entsoe": {
            "api_token": "secret",
            "energy_tax_in": 0.13,
            "energy_tax_out": 0.0,
            "markup_in": 0.02,
            "markup_out": 0.01,
            "vat_in": 0.21,
            "vat_out": 0.0,
        },
    },
}


def test_static_provider_returns_static_fallback():
    provider = PriceProvider(
        PriceSettings(consumption=0.30, delivery=0.08, currency="EUR"),
        event_bus=MagicMock(),
    )

    view = provider.current()
    assert view.price_in == pytest.approx(0.30)
    assert view.price_out == pytest.approx(0.08)
    assert provider.is_dynamic is False


def test_dynamic_provider_falls_back_when_cache_empty():
    provider = PriceProvider(
        PriceSettings(**ENTSOE_DYNAMIC), event_bus=MagicMock()
    )

    view = provider.current()
    assert view.price_in == pytest.approx(0.30)
    assert view.price_out == pytest.approx(0.08)
    assert provider.is_dynamic is True


def test_ingest_applies_markup_tax_vat():
    provider = PriceProvider(
        PriceSettings(**ENTSOE_DYNAMIC), event_bus=MagicMock()
    )

    hour = datetime(2026, 4, 29, 22, tzinfo=timezone.utc)
    # 100 EUR/MWh = 0.10 EUR/kWh spot.
    provider._ingest_spot({hour: 100.0})

    view = provider.current(hour)
    # (0.10 + 0.13 + 0.02) * 1.21 = 0.3025
    assert view.price_in == pytest.approx(0.3025)
    # (0.10 + 0.0 + 0.01) * 1.0 = 0.11
    assert view.price_out == pytest.approx(0.11)


@pytest.mark.asyncio
async def test_fetch_days_writes_points_and_emits_event():
    influxdb = MagicMock()
    influxdb.write_points = AsyncMock()
    event_bus = MagicMock()
    event_bus.emit = AsyncMock()

    provider = PriceProvider(
        PriceSettings(**ENTSOE_DYNAMIC), event_bus=event_bus, influxdb=influxdb
    )

    hour = datetime(2026, 4, 29, 22, tzinfo=timezone.utc)
    provider._client = MagicMock()
    provider._client.fetch_day_ahead = AsyncMock(return_value={hour: 100.0})

    success = await provider._fetch_days([hour.date()])

    assert success is True
    influxdb.write_points.assert_awaited_once()
    written = influxdb.write_points.call_args.args[0]
    assert len(written) == 1

    event_bus.emit.assert_awaited()
    last_event = event_bus.emit.await_args_list[-1].args[0]
    assert last_event.hours == 1


@pytest.mark.asyncio
async def test_fetch_days_partial_success_when_tomorrow_unavailable():
    """Today's prices land even when tomorrow returns Acknowledgement."""
    from solaredge2mqtt.services.prices.client import EntsoePricesUnavailableError

    event_bus = MagicMock()
    event_bus.emit = AsyncMock()
    provider = PriceProvider(PriceSettings(**ENTSOE_DYNAMIC), event_bus=event_bus)

    today = datetime(2026, 4, 29, 22, tzinfo=timezone.utc)

    async def fake_fetch(start, end):
        if start.date() == today.date():
            return {today: 100.0}
        raise EntsoePricesUnavailableError("ack 999: No matching data found")

    provider._client = MagicMock()
    provider._client.fetch_day_ahead = AsyncMock(side_effect=fake_fetch)

    from datetime import timedelta as _td

    success = await provider._fetch_days([today.date(), today.date() + _td(days=1)])

    assert success is True
    assert today in provider._cache
