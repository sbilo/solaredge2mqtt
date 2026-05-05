"""Tests for the ENTSO-E XML parser used by PriceProvider."""

from datetime import datetime, timedelta, timezone

import pytest

from solaredge2mqtt.core.exceptions import InvalidDataException
from solaredge2mqtt.services.prices.client import (
    EntsoePricesUnavailableError,
    parse_day_ahead_xml,
)


def _xml(points_xml: str, start: str = "2026-04-29T22:00Z") -> str:
    return f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<Publication_MarketDocument xmlns=\"urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3\">
  <TimeSeries>
    <Period>
      <timeInterval>
        <start>{start}</start>
        <end>2026-04-30T22:00Z</end>
      </timeInterval>
      <resolution>PT60M</resolution>
      {points_xml}
    </Period>
  </TimeSeries>
</Publication_MarketDocument>
"""


def _point(position: int, amount: float) -> str:
    return f"<Point><position>{position}</position><price.amount>{amount}</price.amount></Point>"


def test_parse_dense_24h_period_returns_hourly_map():
    points = "".join(_point(i + 1, 50.0 + i) for i in range(24))
    prices = parse_day_ahead_xml(_xml(points))

    assert len(prices) == 24
    first_hour = datetime(2026, 4, 29, 22, tzinfo=timezone.utc)
    assert prices[first_hour] == pytest.approx(50.0)
    last_hour = datetime(2026, 4, 30, 21, tzinfo=timezone.utc)
    assert prices[last_hour] == pytest.approx(73.0)


def test_parse_sparse_points_carries_forward_last_price():
    # ENTSO-E may emit only positions where price changes.
    points = _point(1, 30.0) + _point(5, 45.5) + _point(10, 22.0)
    prices = parse_day_ahead_xml(_xml(points))

    expected = {
        1: 30.0,
        2: 30.0,
        3: 30.0,
        4: 30.0,
        5: 45.5,
        6: 45.5,
        7: 45.5,
        8: 45.5,
        9: 45.5,
        10: 22.0,
    }
    base = datetime(2026, 4, 29, 22, tzinfo=timezone.utc)
    for position, value in expected.items():
        hour = base + timedelta(hours=position - 1)
        assert prices[hour] == pytest.approx(value)


def test_parse_skips_non_hourly_resolution():
    body = """<?xml version=\"1.0\"?>
<Publication_MarketDocument xmlns=\"urn:x\">
  <TimeSeries>
    <Period>
      <timeInterval>
        <start>2026-04-29T22:00Z</start><end>2026-04-29T23:00Z</end>
      </timeInterval>
      <resolution>PT15M</resolution>
      <Point><position>1</position><price.amount>1.0</price.amount></Point>
    </Period>
  </TimeSeries>
</Publication_MarketDocument>"""

    with pytest.raises(EntsoePricesUnavailableError):
        parse_day_ahead_xml(body)


def test_parse_invalid_xml_raises():
    with pytest.raises(InvalidDataException):
        parse_day_ahead_xml("not xml at all")
