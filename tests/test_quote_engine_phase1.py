import json
from decimal import Decimal
from pathlib import Path

import pytest

from transfercore.domain.money import CurrencyRegistry
from transfercore.domain.quote_engine import (
    AmountAboveError,
    AmountBelowError,
    QuoteEngine,
    QuoteRequest,
    RateUnavailableError,
    ValidationError,
)


@pytest.fixture(scope="module")
def registry():
    return CurrencyRegistry.from_file("data/currencies.json")


@pytest.fixture(scope="module")
def engine(registry):
    tariffs = json.loads(Path("data/tariffs.json").read_text(encoding="utf-8"))
    rates = json.loads(Path("data/rates.json").read_text(encoding="utf-8"))
    return QuoteEngine(tariffs=tariffs, rates=rates, registry=registry)


def make_req(**overrides) -> QuoteRequest:
    defaults = dict(
        source_country="RU",
        source_currency="RUB",
        target_country="TH",
        target_currency="THB",
        payout_method="BANK_ACCOUNT",
        amount="5000.00",
        amount_type="SOURCE",
    )
    defaults.update(overrides)
    return QuoteRequest(**defaults)


# ---------------------------------------------------------------------------
# TARGET mode and the 2x2 fee matrix
# ---------------------------------------------------------------------------


def test_target_mode_limits_use_computed_source_amount(engine):
    """200000 THB needs ~483442 RUB, above the 300000 RUB direction max."""
    with pytest.raises(AmountAboveError) as exc:
        engine.calculate(make_req(amount="200000.00", amount_type="TARGET"))

    assert exc.value.code == "AMOUNT_ABOVE_MAX"
    assert exc.value.details["max"] == "300000.00"
    assert exc.value.details["currency"] == "RUB"
    assert exc.value.details["actual"] == "483442.11"


def test_target_mode_below_min_uses_computed_source_amount(engine):
    """100 THB needs ~241 RUB, below the 1000 RUB direction min."""
    with pytest.raises(AmountBelowError) as exc:
        engine.calculate(make_req(amount="100.00", amount_type="TARGET"))

    assert exc.value.code == "AMOUNT_BELOW_MIN"
    assert exc.value.details["min"] == "1000.00"
    assert exc.value.details["currency"] == "RUB"
    # 100 THB / 0.42 = 238.10 RUB plus the 50 RUB minimum fee.
    assert exc.value.details["actual"] == "288.10"


def test_target_mode_fee_charged_in_target(engine, registry):
    """RU-VN: TARGET amount + fee charged in TARGET must convert the gross."""
    result = engine.calculate(
        make_req(
            target_country="VN",
            target_currency="VND",
            amount="500000",
            amount_type="TARGET",
        )
    )

    assert result.source_amount.to_str(registry) == "1766.67"
    assert result.fee.to_str(registry) == "30000"
    assert result.fee.currency == "VND"
    assert result.amount_to_convert.to_str(registry) == "1766.67"
    assert result.target_amount.to_str(registry) == "500000"
    assert result.target_amount.currency == "VND"
    assert result.rate == Decimal("300.0000")


def test_target_mode_fee_charged_in_target_below_min(engine):
    """The old bug returned source=100000 RUB and target=354 VND here."""
    with pytest.raises(AmountBelowError) as exc:
        engine.calculate(
            make_req(
                target_country="VN",
                target_currency="VND",
                amount="100000",
                amount_type="TARGET",
            )
        )

    assert exc.value.code == "AMOUNT_BELOW_MIN"
    assert exc.value.details["actual"] == "433.33"


def test_target_mode_fee_charged_in_target_above_max(engine):
    with pytest.raises(AmountAboveError) as exc:
        engine.calculate(
            make_req(
                target_country="VN",
                target_currency="VND",
                amount="100000000",
                amount_type="TARGET",
            )
        )

    assert exc.value.code == "AMOUNT_ABOVE_MAX"
    assert exc.value.details["currency"] == "RUB"


def test_source_target_round_trip_is_consistent(engine, registry):
    source_quote = engine.calculate(make_req(amount="5000.00", amount_type="SOURCE"))
    target_quote = engine.calculate(
        make_req(
            amount=source_quote.target_amount.to_str(registry),
            amount_type="TARGET",
        )
    )

    assert target_quote.source_amount.to_str(registry) == "5000.00"
    assert target_quote.fee.to_str(registry) == "75.00"
    assert target_quote.amount_to_convert.to_str(registry) == "4925.00"
    assert target_quote.target_amount.to_str(registry) == "2068.50"
    assert target_quote.rate == source_quote.rate


def test_target_mode_rounding_is_consistent(engine, registry):
    result = engine.calculate(
        make_req(amount="1000.01", amount_type="TARGET")
    )

    assert result.source_amount.to_str(registry) == "2430.98"
    assert result.fee.to_str(registry) == "50.00"
    assert result.amount_to_convert.to_str(registry) == "2380.98"
    assert result.target_amount.to_str(registry) == "1000.01"
    assert (
        result.amount_to_convert.amount * result.rate
    ).quantize(Decimal("0.01")) == result.target_amount.amount


def test_half_up_rounding_for_zero_decimal_target(registry):
    tariff = {
        "direction_id": "RU-JP-TEST",
        "source": {"country": "RU", "currency": "RUB"},
        "target": {"country": "JP", "currency": "JPY"},
        "payout_method": "BANK_ACCOUNT",
        "fee": {
            "percent": "0.00",
            "fixed": "0.00",
            "min": "0.00",
            "charged_in": "SOURCE",
        },
        "amount_limits": {
            "min": "0.00",
            "max": "1000.00",
            "currency": "RUB",
        },
        "min_margin_percent": "0.00",
        "quote_ttl_sec": 900,
    }
    rate = {
        "base": "RUB",
        "quote": "JPY",
        "rate": "1.9000",
        "updated_at": "2026-11-05T11:46:06Z",
    }
    custom_engine = QuoteEngine(
        tariffs=[tariff], rates=[rate], registry=registry
    )

    result = custom_engine.calculate(
        make_req(
            target_country="JP",
            target_currency="JPY",
            amount="5.00",
        )
    )

    # 5.00 RUB * 1.9000 = 9.50 JPY -> ROUND_HALF_UP to 10 JPY.
    assert result.target_amount.to_str(registry) == "10"


# ---------------------------------------------------------------------------
# Rates: inverse, cross, stale
# ---------------------------------------------------------------------------


def test_inverse_rate_is_used(engine, registry):
    """RUB->KWD has no direct rate; KWD->RUB=270 is inverted."""
    result = engine.calculate(
        make_req(
            target_country="KW",
            target_currency="KWD",
            amount="10000.00",
        )
    )

    assert result.rate == Decimal("0.0037037037")
    assert result.target_amount.to_str(registry) == "36.444"
    assert "inverse of KWD->RUB" in " ".join(result.breakdown)


def test_cross_rate_is_used_when_direct_is_stale(engine, registry):
    """RUB->PHP is stale; a fresh RUB->USD * USD->PHP path exists."""
    result = engine.calculate(
        make_req(
            target_country="PH",
            target_currency="PHP",
            payout_method="CASH_PICKUP",
            amount="10000.00",
        )
    )

    assert result.rate == Decimal("0.6960")
    assert result.target_amount.to_str(registry) == "6786.00"
    assert "cross via RUB->USD * USD->PHP" in " ".join(result.breakdown)


def test_stale_rate_without_fallback_is_rejected(engine):
    """RUB->AED is stale and has no inverse/cross path."""
    with pytest.raises(RateUnavailableError) as exc:
        engine.calculate(
            make_req(
                target_country="AE",
                target_currency="AED",
                amount="10000.00",
            )
        )

    assert exc.value.code == "RATE_UNAVAILABLE"
    assert exc.value.details["reason"] == "stale"
    assert exc.value.details["max_age_seconds"] == 6 * 60 * 60


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"amount": "abc"},
        {"amount": "100.5"},
        {"amount": "100.555"},
        {"amount": "100"},
        {"amount_type": "BOGUS"},
        {"source_currency": "XXX"},
        {"target_currency": "XXX"},
        {"source_country": "RUS"},
        {"source_country": "ru"},
        {"target_currency": "thb"},
    ],
)
def test_invalid_requests_raise_validation_error(engine, overrides):
    with pytest.raises(ValidationError) as exc:
        engine.calculate(make_req(**overrides))

    assert exc.value.code == "VALIDATION_ERROR"


def test_unknown_currency_is_validation_error_not_unsupported_direction(engine):
    with pytest.raises(ValidationError) as exc:
        engine.calculate(make_req(source_currency="XXX"))

    assert exc.value.code == "VALIDATION_ERROR"
    assert exc.value.details["field"] == "source_currency"


def test_zero_amount_is_below_min_not_validation_error(engine):
    with pytest.raises(AmountBelowError) as exc:
        engine.calculate(make_req(amount="0.00"))

    assert exc.value.code == "AMOUNT_BELOW_MIN"


# ---------------------------------------------------------------------------
# Breakdown and determinism
# ---------------------------------------------------------------------------


def test_breakdown_is_populated(engine):
    result = engine.calculate(make_req())

    assert isinstance(result.breakdown, list)
    assert len(result.breakdown) >= 5
    assert all(isinstance(step, str) for step in result.breakdown)
    assert any(step.startswith("Rate:") for step in result.breakdown)
    assert any(step.startswith("Fee:") for step in result.breakdown)
    assert any(step.startswith("Target amount:") for step in result.breakdown)


def test_same_input_produces_same_calculation(engine):
    first = engine.calculate(make_req())
    second = engine.calculate(make_req())

    for field in (
        "direction_id",
        "source_amount",
        "fee",
        "amount_to_convert",
        "rate",
        "target_amount",
        "breakdown",
        "rate_updated_at",
    ):
        assert getattr(first, field) == getattr(second, field)
