import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from transfercore.domain.provider_router import ProviderRouter
from transfercore.infrastructure.rate_feed import load_rate_feed
from transfercore.domain.rates import RateService


async def make_router() -> ProviderRouter:
    rates = await load_rate_feed(Path("data/rates.json"))
    return ProviderRouter(
        json.loads(Path("data/providers.json").read_text(encoding="utf-8")),
        rate_service=RateService(rates),
    )


@pytest.mark.anyio
async def test_ru_th_bank_ranking_uses_score_and_strict_margin():
    router = await make_router()
    ranked = router.select_providers(
        direction_id="RU-TH-BANK",
        target_amount=Decimal("2068.50"),
        target_currency="THB",
        source_amount=Decimal("5000.00"),
        source_currency="RUB",
        client_fee=Decimal("75.00"),
        min_margin_percent=Decimal("0.50"),
    )
    # Alpha wins on success rate and latency. Bravo is the only other provider
    # above the 0.50% margin floor at this amount; charlie/golf are below it,
    # echo is in maintenance and delta is disabled.
    assert ranked == ["prov_alpha", "prov_bravo"]

    evaluations = {
        evaluation.provider_id: evaluation
        for evaluation in router.evaluate(
            direction_id="RU-TH-BANK",
            target_amount=Decimal("2068.50"),
            target_currency="THB",
            source_amount=Decimal("5000.00"),
            source_currency="RUB",
            client_fee=Decimal("75.00"),
            min_margin_percent=Decimal("0.50"),
        )
    }
    assert evaluations["prov_alpha"].is_margin_compliant
    assert evaluations["prov_bravo"].is_margin_compliant
    assert not evaluations["prov_charlie"].is_margin_compliant
    assert not evaluations["prov_golf"].is_margin_compliant
    assert not evaluations["prov_delta"].is_margin_compliant
    assert not evaluations["prov_echo"].is_margin_compliant


@pytest.mark.anyio
async def test_no_silent_fallback_when_every_provider_is_below_minimum_margin():
    router = await make_router()
    ranked = router.select_providers(
        direction_id="RU-TH-BANK",
        target_amount=Decimal("2068.50"),
        target_currency="THB",
        source_amount=Decimal("5000.00"),
        source_currency="RUB",
        client_fee=Decimal("75.00"),
        min_margin_percent=Decimal("2.00"),
    )
    assert ranked == []


@pytest.mark.anyio
async def test_maintenance_window_excludes_provider():
    router = await make_router()
    now = datetime(2026, 11, 8, 22, 10, tzinfo=timezone.utc)
    ranked = router.select_providers(
        direction_id="RU-TH-BANK",
        target_amount=Decimal("2068.50"),
        target_currency="THB",
        source_amount=Decimal("5000.00"),
        source_currency="RUB",
        client_fee=Decimal("75.00"),
        min_margin_percent=Decimal("0.50"),
        now=now,
    )
    assert "prov_alpha" not in ranked
    assert ranked[0] == "prov_bravo"


@pytest.mark.anyio
async def test_provider_limits_are_checked_in_provider_currency():
    router = await make_router()
    ranked = router.select_providers(
        direction_id="RU-TH-BANK",
        target_amount=Decimal("200000.00"),
        target_currency="THB",
        source_amount=Decimal("500000.00"),
        source_currency="RUB",
        client_fee=Decimal("7500.00"),
        min_margin_percent=Decimal("0.50"),
    )
    # 200000 THB is above alpha's 5000 USD and bravo's 3000 EUR limits but
    # inside charlie's 20000 USD limit.
    assert ranked == ["prov_charlie"]


@pytest.mark.anyio
async def test_equal_scores_are_broken_by_provider_id():
    providers = [
        {
            "provider_id": provider_id,
            "directions": ["X-Y"],
            "currency": "RUB",
            "fee": {"percent": "0", "fixed": "1.00"},
            "limits": {"min": "1.00", "max": "1000.00"},
            "success_rate_24h": "0.90",
            "p95_latency_ms": 1000,
            "enabled": True,
        }
        for provider_id in ("prov_zeta", "prov_alpha")
    ]
    router = ProviderRouter(providers, rates_map={})
    ranked = router.select_providers(
        direction_id="X-Y",
        target_amount=Decimal("100.00"),
        target_currency="RUB",
        source_amount=Decimal("1000.00"),
        source_currency="RUB",
        client_fee=Decimal("10.00"),
        min_margin_percent=Decimal("0.50"),
    )
    assert ranked == ["prov_alpha", "prov_zeta"]


@pytest.mark.anyio
async def test_client_fee_is_converted_to_source_currency_before_margin():
    router = await make_router()
    in_rub = router.evaluate(
        direction_id="RU-TH-BANK",
        target_amount=Decimal("2068.50"),
        target_currency="THB",
        source_amount=Decimal("5000.00"),
        source_currency="RUB",
        client_fee=Decimal("75.00"),
        min_margin_percent=Decimal("0.50"),
    )
    in_usd = router.evaluate(
        direction_id="RU-TH-BANK",
        target_amount=Decimal("2068.50"),
        target_currency="THB",
        source_amount=Decimal("5000.00"),
        source_currency="RUB",
        client_fee=Decimal("0.90"),
        min_margin_percent=Decimal("0.50"),
        client_fee_currency="USD",
    )
    rub_margins = {
        e.provider_id: round(e.margin_percent, 6) for e in in_rub
    }
    usd_margins = {
        e.provider_id: round(e.margin_percent, 6) for e in in_usd
    }
    assert usd_margins == rub_margins
