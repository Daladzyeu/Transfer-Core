import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import uuid

from transfercore.domain.money import CurrencyRegistry, Money, registry
from transfercore.domain.rates import (
    RateNotAvailable,
    RateQuote,
    RateService,
    format_timestamp,
)

_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_AMOUNT_TYPES = {"SOURCE", "TARGET"}
_CHARGED_IN = {"SOURCE", "TARGET"}


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
    rate_updated_at: str | None = None


@dataclass(frozen=True)
class Fee:
    percent: Decimal
    fixed: Decimal
    min: Decimal
    charged_in: str

    @classmethod
    def from_dict(cls, data: dict) -> Fee:
        percent = Decimal(data["percent"])
        fixed = Decimal(data["fixed"])
        minimum = Decimal(data["min"])
        charged_in = data["charged_in"]

        if charged_in not in _CHARGED_IN:
            raise ValueError(
                f"fee.charged_in must be SOURCE or TARGET, got {charged_in!r}"
            )
        if percent < 0 or percent >= 100:
            raise ValueError("fee.percent must be in [0, 100)")
        if fixed < 0 or minimum < 0:
            raise ValueError("fee.fixed and fee.min must not be negative")

        return cls(
            percent=percent,
            fixed=fixed,
            min=minimum,
            charged_in=charged_in,
        )


@dataclass(frozen=True)
class Limits:
    min: Decimal
    max: Decimal
    currency: str

    @classmethod
    def from_dict(cls, data: dict) -> Limits:
        return cls(
            min=Decimal(data["min"]),
            max=Decimal(data["max"]),
            currency=data["currency"],
        )


@dataclass(frozen=True)
class Tariff:
    direction_id: str
    source_country: str
    source_currency: str
    target_country: str
    target_currency: str
    payout_method: str
    fee: Fee
    amount_limits: Limits
    min_margin_percent: Decimal
    quote_ttl_sec: int

    @classmethod
    def from_dict(cls, data: dict) -> Tariff:
        return cls(
            direction_id=data["direction_id"],
            source_country=data["source"]["country"],
            source_currency=data["source"]["currency"],
            target_country=data["target"]["country"],
            target_currency=data["target"]["currency"],
            payout_method=data["payout_method"],
            fee=Fee.from_dict(data["fee"]),
            amount_limits=Limits.from_dict(data["amount_limits"]),
            min_margin_percent=Decimal(data["min_margin_percent"]),
            quote_ttl_sec=data["quote_ttl_sec"],
        )


class QuoteError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class ValidationError(QuoteError):
    def __init__(self, message="Invalid request format", details: dict | None = None):
        super().__init__("VALIDATION_ERROR", message, details)


class UnsupportedDirectionError(QuoteError):
    def __init__(
        self,
        message="Unsupported direction or payment method",
        details: dict | None = None,
    ):
        super().__init__(
            "UNSUPPORTED_DIRECTION", message, details
        )  # можно в details добавить словарь с direction {"src": ..., "target":} или с pay meth


class AmountBelowError(QuoteError):
    def __init__(self, min_val: str | Decimal, currency: str, actual: str | None = None):
        min_text = str(min_val)
        details = {"min": min_text, "currency": currency}
        if actual is not None:
            details["actual"] = actual
        super().__init__(
            "AMOUNT_BELOW_MIN",
            f"Amount below minimum: {min_text} {currency}",
            details,
        )


class AmountAboveError(QuoteError):
    def __init__(self, max_val: str | Decimal, currency: str, actual: str | None = None):
        max_text = str(max_val)
        details = {"max": max_text, "currency": currency}
        if actual is not None:
            details["actual"] = actual
        super().__init__(
            "AMOUNT_ABOVE_MAX",
            f"Amount above maximum: {max_text} {currency}",
            details,
        )


class RateUnavailableError(QuoteError):
    def __init__(self, base: str, quote: str, details: dict | None = None):
        super().__init__(
            "RATE_UNAVAILABLE",
            f"No usable exchange rate for: {base}->{quote}",
            details,
        )


# Дописать messages и descriptions + дополнить своими ошибками
class QuoteNotFoundError(QuoteError):
    def __init__(self, message="Quote not found"):
        super().__init__("QUOTE_NOT_FOUND", message)


class QuoteExpiredError(QuoteError):
    def __init__(self, message="Quote expired"):
        super().__init__("QUOTE_EXPIRED", message)


class InsufficientFundsError(QuoteError):
    def __init__(self, message="Insufficient funds"):
        super().__init__("INSUFFICIENT_FUNDS", message)


class DailyLimitExceededError(QuoteError):
    def __init__(self, message="Daily limit exceeded", details: dict | None = None):
        super().__init__("DAILY_LIMIT_EXCEEDED", message, details)


@dataclass(frozen=True)
class _QuoteCalculation:
    source_amount: Decimal
    fee: Decimal
    fee_currency: str
    amount_to_convert: Decimal
    target_amount: Decimal
    breakdown: list[str]


class QuoteEngine:
    def __init__(
        self,
        tariffs: list[Tariff] | list[dict],
        rates: list[dict] | None = None,
        registry: CurrencyRegistry = registry,
        rate_service: RateService | None = None,
    ):
        parsed_tariffs = [
            t if isinstance(t, Tariff) else Tariff.from_dict(t) for t in tariffs
        ]

        self.tariffs = parsed_tariffs
        self.registry = registry
        self.rate_service = rate_service or RateService(rates or [])
        self.rates = self.rate_service.rate_map()
        self._tariffs_by_route = {}  # Пометить где-нибудь в доках, что это для быстрого поиска/проверки доступных direction`ов
        for t in parsed_tariffs:
            key = (
                t.source_country,
                t.source_currency,
                t.target_country,
                t.target_currency,
                t.payout_method,
            )
            self._tariffs_by_route[key] = t

    def calculate(self, request: QuoteRequest) -> QuoteResult:
        amount = self._validate_request(request)
        tariff = self._find_tariff(request)

        try:
            rate_quote = self.rate_service.get_rate(
                request.source_currency, request.target_currency
            )
        except RateNotAvailable as exc:
            raise RateUnavailableError(
                request.source_currency,
                request.target_currency,
                exc.details,
            ) from exc

        calculation = self._calculate_amounts(
            amount=amount,
            amount_type=request.amount_type,
            tariff=tariff,
            rate_quote=rate_quote,
        )
        self._check_limits(calculation.source_amount, tariff)
        self._ensure_positive(calculation)

        dt = datetime.now(timezone.utc) + timedelta(seconds=tariff.quote_ttl_sec)

        result = QuoteResult(
            quote_id=f"q_{uuid.uuid4().hex[:12]}",
            direction_id=tariff.direction_id,
            source_amount=Money(calculation.source_amount, request.source_currency),
            fee=Money(calculation.fee, calculation.fee_currency),
            amount_to_convert=Money(
                calculation.amount_to_convert, request.source_currency
            ),
            rate=rate_quote.rate,
            target_amount=Money(calculation.target_amount, request.target_currency),
            breakdown=calculation.breakdown,
            expires_at=dt.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            rate_updated_at=format_timestamp(rate_quote.updated_at),
        )
        return result

    def _validate_request(self, request: QuoteRequest) -> Decimal:
        if (
            not isinstance(request.amount_type, str)
            or request.amount_type not in _AMOUNT_TYPES
        ):
            raise ValidationError(
                f"amount_type must be SOURCE or TARGET, got {request.amount_type!r}",
                {"field": "amount_type", "allowed": sorted(_AMOUNT_TYPES)},
            )

        for field, value in (
            ("source_country", request.source_country),
            ("target_country", request.target_country),
        ):
            if not isinstance(value, str) or not _COUNTRY_RE.fullmatch(value):
                raise ValidationError(
                    f"{field} must be a 2-letter uppercase country code",
                    {"field": field},
                )

        for field, value in (
            ("source_currency", request.source_currency),
            ("target_currency", request.target_currency),
        ):
            if not isinstance(value, str) or not _CURRENCY_RE.fullmatch(value):
                raise ValidationError(
                    f"{field} must be a 3-letter uppercase currency code",
                    {"field": field},
                )
            try:
                self.registry.get_minor_units(value)
            except ValueError as exc:
                raise ValidationError(
                    f"Unknown currency: {value}",
                    {"field": field, "currency": value},
                ) from exc

        if not isinstance(request.payout_method, str):
            raise ValidationError(
                "payout_method must be a string",
                {"field": "payout_method"},
            )

        if not isinstance(request.amount, str):
            raise ValidationError("amount must be a string", {"field": "amount"})

        amount_currency = (
            request.source_currency
            if request.amount_type == "SOURCE"
            else request.target_currency
        )
        try:
            amount = Money.from_str(
                request.amount, amount_currency, self.registry
            ).amount
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                str(exc),
                {"field": "amount", "currency": amount_currency},
            ) from exc

        return amount

    def _find_tariff(self, request: QuoteRequest) -> Tariff:
        request_key = (
            request.source_country,
            request.source_currency,
            request.target_country,
            request.target_currency,
            request.payout_method,
        )

        tariff = self._tariffs_by_route.get(request_key)
        if not tariff:
            raise UnsupportedDirectionError(
                details={
                    "source_country": request.source_country,
                    "source_currency": request.source_currency,
                    "target_country": request.target_country,
                    "target_currency": request.target_currency,
                    "payout_method": request.payout_method,
                }
            )
        return tariff

    def _calculate_amounts(
        self,
        amount: Decimal,
        amount_type: str,
        tariff: Tariff,
        rate_quote: RateQuote,
    ) -> _QuoteCalculation:
        source_currency = tariff.source_currency
        target_currency = tariff.target_currency
        charged_in = tariff.fee.charged_in
        fee_currency = (
            source_currency if charged_in == "SOURCE" else target_currency
        )
        percent = tariff.fee.percent / Decimal(100)
        rate = rate_quote.rate

        def rounded(value: Decimal, currency: str) -> Decimal:
            return Money.from_decimal(value, currency, self.registry).amount

        def fee_for(base: Decimal) -> Decimal:
            return max(tariff.fee.fixed + base * percent, tariff.fee.min)

        rate_text = str(rate)
        fee_percent_text = str(tariff.fee.percent)
        fee_fixed_text = self._format_amount(tariff.fee.fixed, fee_currency)
        fee_min_text = self._format_amount(tariff.fee.min, fee_currency)
        breakdown = [
            f"Amount type: {amount_type}; fee charged in {charged_in}",
            (
                f"Rate: {rate_text} {source_currency}->{target_currency} "
                f"({rate_quote.path_text}, updated "
                f"{format_timestamp(rate_quote.updated_at)})"
            ),
        ]

        if amount_type == "SOURCE" and charged_in == "SOURCE":
            source_exact = amount
            fee_exact = fee_for(source_exact)
            fee_q = rounded(fee_exact, fee_currency)
            amount_to_convert_q = rounded(
                source_exact - fee_q, source_currency
            )
            source_q = amount_to_convert_q + fee_q
            target_q = rounded(amount_to_convert_q * rate, target_currency)

            breakdown.extend(
                [
                    f"Source amount: {self._format_amount(source_q, source_currency)} {source_currency}",
                    (
                        f"Fee: {self._format_amount(fee_q, fee_currency)} {fee_currency} = "
                        f"max({fee_fixed_text} + {fee_percent_text}% * "
                        f"{self._format_amount(source_exact, source_currency)} {source_currency}, "
                        f"{fee_min_text} {fee_currency})"
                    ),
                    (
                        f"Amount to convert: {self._format_amount(amount_to_convert_q, source_currency)} {source_currency} = "
                        f"{self._format_amount(source_q, source_currency)} - "
                        f"{self._format_amount(fee_q, fee_currency)}"
                    ),
                    (
                        f"Target amount: {self._format_amount(target_q, target_currency)} {target_currency} = "
                        f"{self._format_amount(amount_to_convert_q, source_currency)} * {rate_text}"
                    ),
                ]
            )
        elif amount_type == "SOURCE" and charged_in == "TARGET":
            source_q = amount
            gross_target_exact = source_q * rate
            fee_exact = fee_for(gross_target_exact)
            fee_q = rounded(fee_exact, fee_currency)
            gross_target_q = rounded(gross_target_exact, target_currency)
            target_q = gross_target_q - fee_q
            amount_to_convert_q = source_q

            breakdown.extend(
                [
                    f"Source amount: {self._format_amount(source_q, source_currency)} {source_currency}",
                    (
                        f"Gross target: {self._format_amount(gross_target_q, target_currency)} {target_currency} = "
                        f"{self._format_amount(source_q, source_currency)} * {rate_text}"
                    ),
                    (
                        f"Fee: {self._format_amount(fee_q, fee_currency)} {fee_currency} = "
                        f"max({fee_fixed_text} + {fee_percent_text}% * "
                        f"{self._format_amount(gross_target_exact, target_currency)} {target_currency}, "
                        f"{fee_min_text} {fee_currency})"
                    ),
                    (
                        f"Target amount: {self._format_amount(target_q, target_currency)} {target_currency} = "
                        f"{self._format_amount(gross_target_q, target_currency)} - "
                        f"{self._format_amount(fee_q, fee_currency)}"
                    ),
                ]
            )
        elif amount_type == "TARGET" and charged_in == "SOURCE":
            target_q = amount
            amount_to_convert_exact = target_q / rate
            source_candidate = (
                amount_to_convert_exact + tariff.fee.fixed
            ) / (Decimal(1) - percent)
            fee_candidate = source_candidate - amount_to_convert_exact

            if fee_candidate < tariff.fee.min:
                fee_exact = tariff.fee.min
                source_exact = amount_to_convert_exact + tariff.fee.min
            else:
                fee_exact = fee_candidate
                source_exact = source_candidate

            fee_q = rounded(fee_exact, fee_currency)
            amount_to_convert_q = rounded(
                amount_to_convert_exact, source_currency
            )
            source_q = amount_to_convert_q + fee_q

            breakdown.extend(
                [
                    f"Target amount: {self._format_amount(target_q, target_currency)} {target_currency}",
                    (
                        f"Amount to convert: {self._format_amount(amount_to_convert_q, source_currency)} {source_currency} = "
                        f"{self._format_amount(target_q, target_currency)} / {rate_text}"
                    ),
                    (
                        f"Fee: {self._format_amount(fee_q, fee_currency)} {fee_currency} = "
                        f"max({fee_fixed_text} + {fee_percent_text}% * "
                        f"{self._format_amount(source_exact, source_currency)} {source_currency}, "
                        f"{fee_min_text} {fee_currency})"
                    ),
                    (
                        f"Source amount: {self._format_amount(source_q, source_currency)} {source_currency} = "
                        f"{self._format_amount(amount_to_convert_q, source_currency)} + "
                        f"{self._format_amount(fee_q, fee_currency)}"
                    ),
                ]
            )
        else:  # TARGET + fee charged in TARGET
            target_q = amount
            gross_candidate = (
                target_q + tariff.fee.fixed
            ) / (Decimal(1) - percent)
            fee_candidate = gross_candidate - target_q

            if fee_candidate < tariff.fee.min:
                fee_exact = tariff.fee.min
                gross_exact = target_q + tariff.fee.min
            else:
                fee_exact = fee_candidate
                gross_exact = gross_candidate

            fee_q = rounded(fee_exact, fee_currency)
            gross_q = target_q + fee_q
            amount_to_convert_q = rounded(gross_q / rate, source_currency)
            source_q = amount_to_convert_q

            breakdown.extend(
                [
                    f"Target amount: {self._format_amount(target_q, target_currency)} {target_currency}",
                    (
                        f"Fee: {self._format_amount(fee_q, fee_currency)} {fee_currency} = "
                        f"max({fee_fixed_text} + {fee_percent_text}% * "
                        f"{self._format_amount(gross_exact, target_currency)} {target_currency}, "
                        f"{fee_min_text} {fee_currency})"
                    ),
                    (
                        f"Gross target: {self._format_amount(gross_q, target_currency)} {target_currency} = "
                        f"{self._format_amount(target_q, target_currency)} + "
                        f"{self._format_amount(fee_q, fee_currency)}"
                    ),
                    (
                        f"Amount to convert: {self._format_amount(amount_to_convert_q, source_currency)} {source_currency} = "
                        f"{self._format_amount(gross_q, target_currency)} / {rate_text}"
                    ),
                    f"Source amount: {self._format_amount(source_q, source_currency)} {source_currency}",
                ]
            )

        return _QuoteCalculation(
            source_amount=source_q,
            fee=fee_q,
            fee_currency=fee_currency,
            amount_to_convert=amount_to_convert_q,
            target_amount=target_q,
            breakdown=breakdown,
        )

    def _check_limits(
        self,
        source_amount: Decimal,
        tariff: Tariff,
    ) -> None:
        limit_currency = tariff.amount_limits.currency
        source_currency = tariff.source_currency

        amount_in_limit_currency = source_amount
        if limit_currency != source_currency:
            try:
                amount_in_limit_currency = self.rate_service.convert(
                    source_amount, source_currency, limit_currency
                )
            except RateNotAvailable as exc:
                raise RateUnavailableError(
                    source_currency,
                    limit_currency,
                    exc.details,
                ) from exc

        if amount_in_limit_currency < tariff.amount_limits.min:
            raise AmountBelowError(
                min_val=self._format_amount(
                    tariff.amount_limits.min, limit_currency
                ),
                currency=limit_currency,
                actual=self._format_amount(source_amount, source_currency),
            )

        if amount_in_limit_currency > tariff.amount_limits.max:
            raise AmountAboveError(
                max_val=self._format_amount(
                    tariff.amount_limits.max, limit_currency
                ),
                currency=limit_currency,
                actual=self._format_amount(source_amount, source_currency),
            )

    def _ensure_positive(self, calculation: _QuoteCalculation) -> None:
        for name, value in (
            ("source_amount", calculation.source_amount),
            ("amount_to_convert", calculation.amount_to_convert),
            ("target_amount", calculation.target_amount),
        ):
            if value <= 0:
                raise ValidationError(
                    f"Calculated {name} must be positive; the amount is too small to cover the fee",
                    {"field": "amount", "calculated": name},
                )

        if calculation.fee < 0:
            raise ValidationError(
                "Calculated fee must not be negative",
                {"field": "amount"},
            )

    def _format_amount(self, amount: Decimal, currency: str) -> str:
        return Money.from_decimal(
            amount, currency, self.registry
        ).to_str(self.registry)
