import json
from decimal import Decimal
from pathlib import Path

from transfercore.domain.quote_engine import QuoteEngine, QuoteRequest


def test_quote_engine_example_01():
    # 1. Arrange: Load real data
    tariffs = json.loads(Path("data/tariffs.json").read_text(encoding="utf-8"))
    rates = json.loads(Path("data/rates.json").read_text(encoding="utf-8"))
    engine = QuoteEngine(tariffs=tariffs, rates=rates)

    req = QuoteRequest(
        source_country="RU",
        source_currency="RUB",
        target_country="TH",
        target_currency="THB",
        payout_method="BANK_ACCOUNT",
        amount="5000.00",
        amount_type="SOURCE",
    )

    # 2. Act
    quote = engine.calculate(req)

    # 3. Assert: Verify all values match 01_source_percent_fee.json
    assert quote.direction_id == "RU-TH-BANK"
    assert quote.source_amount.to_str() == "5000.00"
    assert quote.fee.to_str() == "75.00"
    assert quote.amount_to_convert.to_str() == "4925.00"
    assert quote.target_amount.to_str() == "2068.50"
    assert quote.rate == Decimal("0.4200")
