import json
from typing import Mapping
from pathlib import Path
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

DEFAULT_CURRENCIES: dict[str, int] = {
    "RUB": 2,
    "USD": 2,
    "EUR": 2,
    "JPY": 0,
    "VND": 0,
    "KWD": 3,
}


class CurrencyRegistry:
    def __init__(self, precisions: Mapping[str, int] | None = None):
        self._precisions: dict[str, int] = dict(precisions or DEFAULT_CURRENCIES)

    @classmethod
    def from_file(cls, path: Path | str = "data/currencies.json") -> CurrencyRegistry:
        json_path = Path(path)
        if not json_path.exists():
            return cls(DEFAULT_CURRENCIES)
        data = json.loads(json_path.read_text(encoding="utf-8"))
        precisions = {item["code"]: int(item["minor_units"]) for item in data}
        return cls(precisions)

    def get_minor_units(self, currency: str) -> int:
        if currency not in self._precisions:
            raise ValueError(f"Unknown currency: {currency}")
        return self._precisions[currency]


registry = CurrencyRegistry().from_file()


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str

    def __post_init__(self):
        if not isinstance(self.amount, Decimal):
            raise TypeError(f"amount must be Decimal, but got {type(self.amount)}")
        if not isinstance(self.currency, str) or len(self.currency) != 3:
            raise ValueError(f"Invalid currency code: {self.currency}")

    @classmethod
    def from_str(
        cls, amount_text: str, currency: str, reg: CurrencyRegistry = registry
    ) -> Money:
        minor_units = reg.get_minor_units(currency)
        if not isinstance(amount_text, str):
            raise TypeError(f"Amount must be a string")
        whole, dot, fraction = amount_text.partition(".")

        if not whole.isdigit():
            raise ValueError(f"Invalid integer part in amount: {amount_text}")

        if minor_units == 0:
            if dot != "":
                raise ValueError(
                    f"Currency {currency} has 0 minor units and cannot have decimals: {amount_text}"
                )
        else:
            if dot != "." or len(fraction) != minor_units or not fraction.isdigit():
                raise ValueError(
                    f"Amount {amount_text} must have exactly {minor_units} decimal digits for currency {currency}"
                )

        try:
            value = Decimal(amount_text)
        except InvalidOperation:
            raise ValueError(f"Invalid decimal string: {amount_text}")
        return cls(amount=value, currency=currency)

    @classmethod
    def from_decimal(
        cls,
        amount: Decimal,
        currency: str,
        reg: CurrencyRegistry = registry,
        rounding=ROUND_HALF_UP,  # пометить ASSUMPTIONS.md и DECISIONS.md
    ) -> Money:
        minor_units = reg.get_minor_units(currency)
        exponent = Decimal(10) ** -minor_units
        quantized = amount.quantize(exponent, rounding=rounding)
        return cls(amount=quantized, currency=currency)

    def to_str(self, reg: CurrencyRegistry = registry) -> str:
        minor_units = reg.get_minor_units(self.currency)
        exponent = Decimal(10) ** -minor_units
        quantized = self.amount.quantize(exponent, rounding=ROUND_HALF_UP)
        if minor_units == 0:
            return str(int(quantized))
        return f"{quantized:.{minor_units}f}"

    def __add__(self, other: Money) -> Money | None:
        if self.currency != other.currency:
            raise TypeError("Currency mismatch")
        return Money(self.amount + other.amount, self.currency)
