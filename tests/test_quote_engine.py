import json
from decimal import Decimal
from pathlib import Path
import pytest

from transfercore.domain.quote_engine import (
    QuoteEngine,
    QuoteRequest,
    QuoteError,
)


@pytest.fixture(scope="module")
def engine():
    tariffs = json.loads(Path("data/tariffs.json").read_text(encoding="utf-8"))
    rates = json.loads(Path("data/rates.json").read_text(encoding="utf-8"))
    return QuoteEngine(tariffs=tariffs, rates=rates)


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
    assert result.amount_to_convert.to_str() == expected_body["amount_to_convert"]["amount"]
    assert result.amount_to_convert.currency == expected_body["amount_to_convert"]["currency"]

    # 5. Verify rate
    assert result.rate == Decimal(expected_body["rate"])

    # 6. Verify target_amount
    assert result.target_amount.to_str() == expected_body["target_amount"]["amount"]
    assert result.target_amount.currency == expected_body["target_amount"]["currency"]
