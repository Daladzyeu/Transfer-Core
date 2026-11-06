from dataclasses import dataclass
from decimal import Decimal

from transfercore.domain.money import CurrencyRegistry, Money, registry


@dataclass(frozen=True)
class QuoteRequest:
    source_country: str
    source_currency: str
    target_country: str
    target_currency: str
    payout_method: str
    amount: str
    amount_type: str


@dataclass(frozen=True)
class QuoteResult:
    quote_id: str
    direction_id: str
    source_amount: Money
    fee: Money
    amount_to_convert: Money
    rate: Decimal
    target_amount: Money
    breakdown: list[str] | None
    expires_at: str


class QuoteError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class ValidationError(QuoteError):
    def __init__(self, message="Invalid request format"):
        super().__init__("VALIDATION_ERROR", message)


class UnsupportedDirectionError(QuoteError):
    def __init__(self, message="Unsupported direction or payment method"):
        super().__init__(
            "UNSUPPORTED DIRECTION", message
        )  # можно в details добавить словарь с direction {"src": ..., "target":} или с pay meth


class AmountBelowError(QuoteError):
    def __init__(self, min_val: str, currency: str):
        super().__init__(
            "AMOUNT_BELOW_MIN",
            f"Amount below minimum: {min_val} {currency}",
            {"min": min_val},
        )


class AmountAboveMax(QuoteError):
    def __init__(self, max_val: str, currency: str):
        super().__init__(
            "AMOUNT_ABOVE_MAX", f"Amount above maximum: {max_val}", {"max": max_val}
        )


class RateUnavailableError(QuoteError):
    def __init__(self, base: str, quote: str):
        super().__init__("RATE_UNAVAILABLE", f"No exchange rate for: {base}->{quote}")


class QuoteNotFoundError(QuoteError):
    def __init__(self, message="Amount is above max"):
        super().__init__("AMOUNT_ABOVE_MAX", message)


class QuoteExpiredError(QuoteError):
    def __init__(self, message="Amount is above max"):
        super().__init__("AMOUNT_ABOVE_MAX", message)


class InsufficientFundsError(QuoteError):
    def __init__(self, message="Amount is above max"):
        super().__init__("AMOUNT_ABOVE_MAX", message)


class DailyLimitExceededError(QuoteError):
    def __init__(self, message="Amount is above max"):
        super().__init__("AMOUNT_ABOVE_MAX", message)


class QuoteEngine:
    def __init__(
        self,
        tariffs: list[dict],
        rates: list[dict],
        registry: CurrencyRegistry = registry,
    ):
        self.tariffs = tariffs
        self.rates = {(r["base"], r["quote"]): Decimal(r["rate"]) for r in rates}
        self.registry = registry
        self._tariffs_by_route = {}  # Пометить где-нибудь в доках, что это для быстрого поиска/проверки доступных direction`ов
        for t in tariffs:
            key = (
                t["source"]["country"],
                t["source"]["currency"],
                t["target"]["country"],
                t["target"]["currency"],
                t["payout_method"],
            )
            self._tariffs_by_route[key] = t

    def calculate(self, request: QuoteRequest) -> QuoteResult:
        request_key = (
            request.source_country,
            request.source_currency,
            request.target_country,
            request.target_currency,
            request.payout_method,
        )
        tariff = self._tariffs_by_route.get(request_key)
        if not tariff:
            raise UnsupportedDirectionError()
        rate = self.rates.get((request.source_currency, request.target_currency))
        if not rate:
            raise RateUnavailableError(
                base=request.source_currency, quote=request.target_currency
            )

        if Decimal(request.amount) < Decimal(
            tariff["amount_limits"]["min"]
        ):  # обернуть в Decimal иначе не пройдет
            raise AmountBelowError(
                min_val=tariff["amount_limits"]["min"],
                currency=tariff["source"]["currency"],
            )

        if Decimal(request.amount) > Decimal(tariff["amount_limits"]["max"]):
            raise AmountAboveMax(
                max_val=tariff["amount_limits"]["max"],
                currency=tariff["source"]["currency"],
            )  # не забыть название класса переименовать

        # if request.amount_type == tariff["fee"]["charged_in"]:
        #    if request.amount_type == "SOURCE":
        #         raw_fee = (
        #             tariff["fee"]["fixed"]
        #            + Decimal(request.amount) * Decimal(tariff["fee"]["percent"])
        #       )  # можно обернуть данные с json по датаклассам типа fee, tariff, limits где они сразу в Decimal будут
        #       fee = min(raw_fee, tariff["fee"]["fixed"])
        #       amount_to_convert = request.amount - fee
        #      target_amount = amount_to_convert * rate
        # else:
        #     pass
        #    else:
        #        if request.amount_type == "SOURCE":
        #           pass
        #      else:
        #          pass
        raw_fee = (
            Decimal(tariff["fee"]["fixed"])
            + Decimal(request.amount) * Decimal(tariff["fee"]["percent"])
        )  # можно обернуть данные с json по датаклассам типа fee, tariff, limits где они сразу в Decimal будут
        fee = max(raw_fee, Decimal(tariff["fee"]["fixed"]))
        amount_to_convert = Decimal(request.amount) - fee
        target_amount = amount_to_convert * rate

        result = QuoteResult(
            quote_id="q_test_123",
            direction_id=tariff["direction_id"],
            source_amount=Money.from_decimal(
                Decimal(request.amount), request.source_currency, self.registry
            ),
            fee=Money.from_decimal(fee, request.source_currency, self.registry),
            amount_to_convert=Money.from_decimal(
                amount_to_convert, request.source_currency, self.registry
            ),
            rate=rate,
            target_amount=Money.from_decimal(
                target_amount, request.target_currency, self.registry
            ),
            breakdown=[f"fee: {fee}", f"rate: {rate}"],
            expires_at="2026-11-06T12:00:00Z",
        )
        return result
