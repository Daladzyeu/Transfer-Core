from decimal import Decimal
from pathlib import Path
import pytest

from transfercore.domain.money import CurrencyRegistry, Money

# --- Mock Data & Fixtures ---

MOCK_CURRENCIES = {
    "RUB": 2,
    "USD": 2,
    "EUR": 2,
    "JPY": 0,
    "VND": 0,
    "KWD": 3,
}


@pytest.fixture
def mock_registry():
    """Returns a CurrencyRegistry with known minor units for testing."""
    return CurrencyRegistry(MOCK_CURRENCIES)


# --- Tests for CurrencyRegistry ---


def test_registry_lookup(mock_registry):
    assert mock_registry.get_minor_units("RUB") == 2
    assert mock_registry.get_minor_units("JPY") == 0
    assert mock_registry.get_minor_units("KWD") == 3


def test_registry_unknown_currency(mock_registry):
    with pytest.raises(ValueError, match="Unknown currency: XYZ"):
        mock_registry.get_minor_units("XYZ")


def test_registry_from_real_data_file():
    """Verify loading the real currencies.json from the data folder."""
    data_path = Path("data/currencies.json")
    if data_path.exists():
        reg = CurrencyRegistry.from_file(data_path)
        assert reg.get_minor_units("RUB") == 2
        assert reg.get_minor_units("JPY") == 0
        assert reg.get_minor_units("VND") == 0
        assert reg.get_minor_units("KWD") == 3


# --- Tests for Money Initialization & Safeguards ---


def test_money_creation_success():
    m = Money(amount=Decimal("100.50"), currency="RUB")
    assert m.amount == Decimal("100.50")
    assert m.currency == "RUB"


def test_money_rejects_float():
    """FinTech rule: float must never be accepted as Money amount."""
    with pytest.raises(TypeError, match="amount must be Decimal"):
        Money(amount=100.50, currency="RUB")  # type: ignore


def test_money_rejects_invalid_currency_code():
    with pytest.raises(ValueError, match="Invalid currency code"):
        Money(amount=Decimal("100.00"), currency="RU")  # length != 3

    with pytest.raises(ValueError, match="Invalid currency code"):
        Money(amount=Decimal("100.00"), currency="RUBLE")  # length != 3


# --- Tests for from_decimal and Quantization ---


def test_from_decimal_rounding_half_up(mock_registry):
    # 10.555 with 2 decimals should round UP to 10.56
    m = Money.from_decimal(Decimal("10.555"), "RUB", reg=mock_registry)
    assert m.amount == Decimal("10.56")

    # 10.554 with 2 decimals should round DOWN to 10.55
    m2 = Money.from_decimal(Decimal("10.554"), "RUB", reg=mock_registry)
    assert m2.amount == Decimal("10.55")


def test_from_decimal_preserves_trailing_zeros(mock_registry):
    # Decimal("10.5") -> should become Decimal("10.50")
    m = Money.from_decimal(Decimal("10.5"), "RUB", reg=mock_registry)
    assert m.amount == Decimal("10.50")


def test_from_decimal_zero_decimals_currency(mock_registry):
    # JPY has 0 minor units
    m = Money.from_decimal(Decimal("18658.75"), "JPY", reg=mock_registry)
    assert m.amount == Decimal("18659")


def test_from_decimal_three_decimals_currency(mock_registry):
    # KWD has 3 minor units
    m = Money.from_decimal(Decimal("50.1"), "KWD", reg=mock_registry)
    assert m.amount == Decimal("50.100")


# --- Tests for to_str Formatting ---


@pytest.mark.parametrize(
    "amount, currency, expected_str",
    [
        (Decimal("10000.00"), "RUB", "10000.00"),
        (Decimal("150.50"), "USD", "150.50"),
        (Decimal("18658"), "JPY", "18658"),  # 0 decimals: no dot
        (Decimal("2069800"), "VND", "2069800"),  # 0 decimals: no dot
        (Decimal("50.000"), "KWD", "50.000"),  # 3 decimals
        (Decimal("1.125"), "KWD", "1.125"),  # 3 decimals
    ],
)
def test_to_str_exact_formatting(mock_registry, amount, currency, expected_str):
    m = Money(amount=amount, currency=currency)
    assert m.to_str(reg=mock_registry) == expected_str


def test_to_str_uses_passed_registry_not_global():
    reg = CurrencyRegistry({"RUB": 4})
    m = Money(amount=Decimal("1.23456"), currency="RUB")
    assert m.to_str(reg) == "1.2346"


# --- Tests for from_str String Parsing ---


def test_from_str_valid_two_decimals(mock_registry):
    m = Money.from_str("10000.00", "RUB", reg=mock_registry)
    assert m.amount == Decimal("10000.00")
    assert m.currency == "RUB"


def test_from_str_valid_zero_decimals(mock_registry):
    m = Money.from_str("18658", "JPY", reg=mock_registry)
    assert m.amount == Decimal("18658")
    assert m.currency == "JPY"


def test_from_str_valid_three_decimals(mock_registry):
    m = Money.from_str("50.000", "KWD", reg=mock_registry)
    assert m.amount == Decimal("50.000")
    assert m.currency == "KWD"


def test_from_str_rejects_non_string(mock_registry):
    with pytest.raises(TypeError, match="Amount must be a string"):
        Money.from_str(100.50, "RUB", reg=mock_registry)  # type: ignore


def test_from_str_rejects_missing_decimals(mock_registry):
    # RUB requires 2 decimal places. "100.5" is invalid
    with pytest.raises(ValueError, match="must have exactly 2 decimal digits"):
        Money.from_str("100.5", "RUB", reg=mock_registry)


def test_from_str_rejects_excess_decimals(mock_registry):
    # RUB requires 2 decimal places. "100.555" is invalid
    with pytest.raises(ValueError, match="must have exactly 2 decimal digits"):
        Money.from_str("100.555", "RUB", reg=mock_registry)


def test_from_str_rejects_dots_in_zero_decimal_currency(mock_registry):
    # JPY has 0 minor units, so "18658.00" must be rejected
    with pytest.raises(ValueError, match="cannot have decimals"):
        Money.from_str("18658.00", "JPY", reg=mock_registry)


# --- Tests for Arithmetic Operations ---


def test_money_addition_same_currency():
    m1 = Money(Decimal("100.25"), "RUB")
    m2 = Money(Decimal("50.50"), "RUB")
    total = m1 + m2
    assert total.amount == Decimal("150.75")
    assert total.currency == "RUB"


def test_money_addition_currency_mismatch():
    m1 = Money(Decimal("100.00"), "RUB")
    m2 = Money(Decimal("50.00"), "USD")
    with pytest.raises(TypeError, match="Currency mismatch"):
        _ = m1 + m2
