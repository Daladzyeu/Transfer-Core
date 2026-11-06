import json
from decimal import Decimal
from pathlib import Path
import pytest
from datetime import datetime, timedelta, timezone

from transfercore.domain.money import CurrencyRegistry
from transfercore.domain.quote_engine import (
    QuoteEngine,
    QuoteRequest,
    QuoteError,
    QuoteExpiredError,
    UnsupportedDirectionError,
    RateUnavailableError,
    QuoteNotFoundError,
    AmountAboveError,
    AmountBelowError,
    ValidationError,
    DailyLimitExceededError,
    InsufficientFundsError,
)
from transfercore.domain.storage import InMemoryQuoteStore


@pytest.fixture(scope="module")
def engine():
    tariffs = json.loads(Path("data/tariffs.json").read_text(encoding="utf-8"))
    rates = json.loads(Path("data/rates.json").read_text(encoding="utf-8"))
    reg = CurrencyRegistry.from_file("data/currencies.json")
    return QuoteEngine(tariffs=tariffs, rates=rates, registry=reg)


# Load all example files sorted by filename
EXAMPLES_DIR = Path("examples")
EXAMPLE_FILES = sorted(EXAMPLES_DIR.glob("*.json"))


@pytest.mark.parametrize("example_path", EXAMPLE_FILES, ids=lambda p: p.stem)
def test_quote_engine_against_examples(engine, example_path):
    data = json.loads(example_path.read_text(encoding="utf-8"))
    req_data = data["request"]
    resp_data = data["response"]

    req = QuoteRequest(
        source_country=req_data["source_country"],
        source_currency=req_data["source_currency"],
        target_country=req_data["target_country"],
        target_currency=req_data["target_currency"],
        payout_method=req_data["payout_method"],
        amount=req_data["amount"],
        amount_type=req_data["amount_type"],
    )

    # Error case: expected response has "error"
    if "error" in resp_data.get("body", {}):
        expected_code = resp_data["body"]["error"]["code"]
        with pytest.raises(QuoteError) as exc_info:
            engine.calculate(req)
        assert exc_info.value.code == expected_code, (
            f"Expected error code '{expected_code}', got '{exc_info.value.code}'"
        )
        return

    # Success case: expected 201 response with quote body
    expected_body = resp_data["body"]
    result = engine.calculate(req)

    # 1. Verify direction_id
    assert result.direction_id == expected_body["direction_id"]

    # 2. Verify source_amount
    assert result.source_amount.to_str() == expected_body["source_amount"]["amount"]
    assert result.source_amount.currency == expected_body["source_amount"]["currency"]

    # 3. Verify fee amount and currency
    assert result.fee.to_str() == expected_body["fee"]["amount"]
    assert result.fee.currency == expected_body["fee"]["currency"]

    # 4. Verify amount_to_convert
    assert (
        result.amount_to_convert.to_str()
        == expected_body["amount_to_convert"]["amount"]
    )
    assert (
        result.amount_to_convert.currency
        == expected_body["amount_to_convert"]["currency"]
    )

    # 5. Verify rate
    assert result.rate == Decimal(expected_body["rate"])

    # 6. Verify target_amount
    assert result.target_amount.to_str() == expected_body["target_amount"]["amount"]
    assert result.target_amount.currency == expected_body["target_amount"]["currency"]


def make_req(**overrides) -> QuoteRequest:
    """Base valid RU->TH BANK_ACCOUNT SOURCE request. Override any field."""
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


# =============================================================================
# 1. UNSUPPORTED_DIRECTION
# =============================================================================


def test_unsupported_direction_wrong_payout_method(engine):
    """CASH_PICKUP is not in tariffs -> UNSUPPORTED_DIRECTION."""
    with pytest.raises(QuoteError) as exc:
        engine.calculate(make_req(payout_method="CASH_PICKUP"))
    assert exc.value.code == "UNSUPPORTED_DIRECTION"


def test_unsupported_direction_unknown_country_pair(engine):
    """US->RU corridor does not exist in tariffs -> UNSUPPORTED_DIRECTION."""
    with pytest.raises(QuoteError) as exc:
        engine.calculate(
            make_req(
                source_country="US",
                source_currency="USD",
                target_country="RU",
                target_currency="RUB",
            )
        )
    assert exc.value.code == "UNSUPPORTED_DIRECTION"


# =============================================================================
# 2. AMOUNT_BELOW_MIN
# =============================================================================


def test_amount_below_min_source_mode(engine):
    """RU-TH-BANK has min=1000.00 RUB. Sending 500.00 must fail."""
    with pytest.raises(QuoteError) as exc:
        engine.calculate(make_req(amount="500.00"))
    assert exc.value.code == "AMOUNT_BELOW_MIN"


def test_amount_below_min_has_details(engine):
    """Error details must include the min limit value."""
    with pytest.raises(AmountBelowError) as exc:
        engine.calculate(make_req(amount="500.00"))
    assert "min" in exc.value.details


# =============================================================================
# 3. AMOUNT_ABOVE_MAX
# =============================================================================


def test_amount_above_max_source_mode(engine):
    """RU-TH-BANK has max=300000.00 RUB. Sending 600000.00 must fail."""
    with pytest.raises(QuoteError) as exc:
        engine.calculate(make_req(amount="600000.00"))
    assert exc.value.code == "AMOUNT_ABOVE_MAX"


def test_amount_above_max_has_details(engine):
    """Error details must include the max limit value."""
    with pytest.raises(AmountAboveError) as exc:
        engine.calculate(make_req(amount="600000.00"))
    assert "max" in exc.value.details


# =============================================================================
# 4. RATE_UNAVAILABLE
# =============================================================================


def test_rate_unavailable_for_direction(engine):
    """
    KZ-TH-BANK (KZT->THB) is a valid tariff direction but has no exchange
    rate in rates.json -> RATE_UNAVAILABLE.
    """
    with pytest.raises(QuoteError) as exc:
        engine.calculate(
            QuoteRequest(
                source_country="KZ",
                source_currency="KZT",
                target_country="TH",
                target_currency="THB",
                payout_method="BANK_ACCOUNT",
                amount="10000.00",
                amount_type="SOURCE",
            )
        )
    assert exc.value.code == "RATE_UNAVAILABLE"


# =============================================================================
# 5. QUOTE_NOT_FOUND  (via storage)
# =============================================================================


def test_quote_not_found():
    """Retrieving a non-existent quote_id raises QUOTE_NOT_FOUND."""
    store = InMemoryQuoteStore()
    with pytest.raises(QuoteError) as exc:
        store.get("non_existent_id_xyz")
    assert exc.value.code == "QUOTE_NOT_FOUND"


# =============================================================================
# 6. QUOTE_EXPIRED  (via storage with injected time)
# =============================================================================


def test_quote_expired(engine):
    """
    A quote saved with a TTL and then retrieved after the TTL elapses
    must raise QUOTE_EXPIRED.
    """
    store = InMemoryQuoteStore()
    quote = engine.calculate(make_req())
    store.save(quote)

    # Parse the quote's real expiry and push time 1 second past it
    expires_at = datetime.fromisoformat(quote.expires_at.replace("Z", "+00:00"))
    future = expires_at + timedelta(seconds=1)

    with pytest.raises(QuoteError) as exc:
        store.get(quote.quote_id, now=future)
    assert exc.value.code == "QUOTE_EXPIRED"


def test_quote_not_expired_before_ttl(engine):
    """A quote retrieved just before its TTL must still be valid."""
    store = InMemoryQuoteStore()
    quote = engine.calculate(make_req())
    store.save(quote)

    expires_at = datetime.fromisoformat(quote.expires_at.replace("Z", "+00:00"))
    just_before = expires_at - timedelta(seconds=1)

    # Must NOT raise
    result = store.get(quote.quote_id, now=just_before)
    assert result.quote_id == quote.quote_id


# =============================================================================
# 7. QuoteError is base class for all errors
# =============================================================================


def test_all_errors_are_quote_errors(engine):
    """All domain errors must be catchable via the single QuoteError base class."""
    errors = []

    for bad_req, label in [
        (make_req(payout_method="CASH_PICKUP"), "UNSUPPORTED_DIRECTION"),
        (make_req(amount="500.00"), "AMOUNT_BELOW_MIN"),
        (make_req(amount="600000.00"), "AMOUNT_ABOVE_MAX"),
    ]:
        with pytest.raises(QuoteError) as exc:
            engine.calculate(bad_req)
        errors.append(exc.value.code)

    assert errors == ["UNSUPPORTED_DIRECTION", "AMOUNT_BELOW_MIN", "AMOUNT_ABOVE_MAX"]
