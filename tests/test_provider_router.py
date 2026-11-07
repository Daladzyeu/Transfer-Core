import json
from decimal import Decimal
from pathlib import Path
import pytest

from transfercore.domain.provider_router import CurrencyConverter, ProviderRouter


def test_currency_converter():
    rates_map = {
        ("RUB", "USD"): Decimal("0.01"),
        ("USD", "THB"): Decimal("35.00"),
    }
    conv = CurrencyConverter(rates_map)

    # Same currency
    assert conv.convert(Decimal("100"), "RUB", "RUB") == Decimal("100")
    # Direct
    assert conv.convert(Decimal("100"), "RUB", "USD") == Decimal("1.00")
    # Inverse
    assert conv.convert(Decimal("1"), "USD", "RUB") == Decimal("100")
    # Cross rate
    assert conv.convert(Decimal("100"), "RUB", "THB") == Decimal("35.00")


def test_provider_router_selection():
    providers = json.loads(Path("data/providers.json").read_text(encoding="utf-8"))
    rates = json.loads(Path("data/rates.json").read_text(encoding="utf-8"))
    rates_map = {(r["base"], r["quote"]): Decimal(r["rate"]) for r in rates}

    router = ProviderRouter(providers, rates_map)

    # RU-TH-BANK
    ranked = router.select_providers(
        direction_id="RU-TH-BANK",
        target_amount=Decimal("4137.00"),
        target_currency="THB",
        source_amount=Decimal("10000.00"),
        source_currency="RUB",
        client_fee=Decimal("150.00"),
        min_margin_percent=Decimal("0.50"),
    )

    assert len(ranked) > 0
    # Must contain providers that support RU-TH-BANK
    assert any("prov" in pid for pid in ranked)
