import json
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from transfercore.domain.rates import RateNotAvailable, RateService
from transfercore.infrastructure.rate_feed import load_rate_feed


def rate(base: str, quote: str, value: str, updated_at: str) -> dict:
    return {
        "base": base,
        "quote": quote,
        "rate": value,
        "updated_at": updated_at,
    }


RATES = [
    rate("AAA", "BBB", "2.0", "2026-01-01T12:00:00Z"),
    rate("BBB", "CCC", "3.0", "2026-01-01T06:00:00Z"),
    rate("CCC", "DDD", "4.0", "2026-01-01T05:59:59Z"),
]


def test_direct_rate_is_preferred():
    service = RateService(RATES)
    quote = service.get_rate("AAA", "BBB")

    assert quote.rate == Decimal("2.0")
    assert quote.kind == "direct"
    assert quote.path_text == "AAA->BBB"


def test_inverse_rate_is_used_when_direct_is_missing():
    service = RateService(RATES)
    quote = service.get_rate("BBB", "AAA")

    assert quote.rate == Decimal("0.5000")
    assert quote.kind == "inverse"
    assert quote.path_text == "inverse of AAA->BBB"


def test_cross_rate_via_one_intermediate():
    service = RateService(RATES)
    quote = service.get_rate("AAA", "CCC")

    assert quote.rate == Decimal("6.0000")
    assert quote.kind == "cross"
    assert quote.path_text == "cross via AAA->BBB * BBB->CCC"


def test_rate_exactly_six_hours_old_is_fresh():
    service = RateService(RATES)
    assert service.get_rate("BBB", "CCC").rate == Decimal("3.0")


def test_rate_older_than_six_hours_is_stale():
    service = RateService(RATES)

    with pytest.raises(RateNotAvailable) as exc:
        service.get_rate("CCC", "DDD")

    assert exc.value.reason == "stale"
    assert exc.value.details["reason"] == "stale"
    assert exc.value.details["max_age_seconds"] == 6 * 60 * 60
    assert exc.value.details["updated_at"] == "2026-01-01T05:59:59Z"
    assert exc.value.details["newest_updated_at"] == "2026-01-01T12:00:00Z"


def test_missing_rate_is_reported_as_missing():
    service = RateService(RATES)

    with pytest.raises(RateNotAvailable) as exc:
        service.get_rate("AAA", "DDD")

    assert exc.value.reason == "missing"


def test_convert_uses_the_same_rate_resolution():
    service = RateService(RATES)

    assert service.convert(Decimal("10"), "AAA", "BBB") == Decimal("20.0")
    assert service.convert(Decimal("10"), "AAA", "AAA") == Decimal("10")


def test_rate_map_contains_all_pairs():
    service = RateService(RATES)
    rates_map = service.rate_map()

    assert rates_map[("AAA", "BBB")] == Decimal("2.0")
    assert rates_map[("CCC", "DDD")] == Decimal("4.0")


def test_empty_feed_identity_uses_injected_clock():
    fixed_now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    service = RateService([], clock=lambda: fixed_now)

    quote = service.get_rate("AAA", "AAA")

    assert quote.rate == Decimal("1")
    assert quote.updated_at == fixed_now


@pytest.mark.anyio
async def test_load_rate_feed_uses_live_response(tmp_path):
    snapshot = tmp_path / "rates.json"
    snapshot.write_text("[]", encoding="utf-8")
    live = [rate("AAA", "BBB", "2.0", "2026-01-01T12:00:00Z")]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=live)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        feed = await load_rate_feed(
            snapshot, url="http://test/rates", client=client
        )

    assert feed == live


@pytest.mark.anyio
async def test_load_rate_feed_falls_back_on_http_error(tmp_path):
    snapshot_data = [rate("AAA", "BBB", "2.0", "2026-01-01T12:00:00Z")]
    snapshot = tmp_path / "rates.json"
    snapshot.write_text(json.dumps(snapshot_data), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        feed = await load_rate_feed(
            snapshot, url="http://test/rates", client=client
        )

    assert feed == snapshot_data


@pytest.mark.anyio
async def test_load_rate_feed_falls_back_on_invalid_payload(tmp_path):
    snapshot_data = [rate("AAA", "BBB", "2.0", "2026-01-01T12:00:00Z")]
    snapshot = tmp_path / "rates.json"
    snapshot.write_text(json.dumps(snapshot_data), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        feed = await load_rate_feed(
            snapshot, url="http://test/rates", client=client
        )

    assert feed == snapshot_data


@pytest.mark.anyio
async def test_load_rate_feed_can_force_snapshot(tmp_path):
    snapshot_data = [rate("AAA", "BBB", "2.0", "2026-01-01T12:00:00Z")]
    snapshot = tmp_path / "rates.json"
    snapshot.write_text(json.dumps(snapshot_data), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("snapshot mode must not perform an HTTP request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        feed = await load_rate_feed(snapshot, url="snapshot", client=client)

    assert feed == snapshot_data
