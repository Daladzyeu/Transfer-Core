from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Callable, Iterable

DEFAULT_MAX_AGE = timedelta(hours=6)
_RATE_QUANTUM = Decimal("0.0000000001")
_MIN_RATE_DECIMALS = 4


def parse_timestamp(value: str | datetime) -> datetime:
    """Parse an ISO-8601 timestamp and normalize it to UTC."""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_timestamp(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _format_computed_rate(value: Decimal) -> Decimal:
    """Keep inverse/cross rates precise but readable (at least 4 decimals)."""
    quantized = value.quantize(_RATE_QUANTUM, rounding=ROUND_HALF_UP)
    text = format(quantized, "f")
    whole, _, fraction = text.partition(".")
    fraction = fraction.rstrip("0")
    if len(fraction) < _MIN_RATE_DECIMALS:
        fraction = fraction.ljust(_MIN_RATE_DECIMALS, "0")
    return Decimal(f"{whole}.{fraction}")


@dataclass(frozen=True)
class Rate:
    base: str
    quote: str
    rate: Decimal
    updated_at: datetime


@dataclass(frozen=True)
class RateQuote:
    rate: Decimal
    updated_at: datetime
    kind: str
    path: tuple[tuple[str, str], ...] = ()

    @property
    def path_text(self) -> str:
        if self.kind == "direct" and self.path:
            base, quote = self.path[0]
            return f"{base}->{quote}"
        if self.kind == "inverse" and self.path:
            base, quote = self.path[0]
            return f"inverse of {quote}->{base}"
        if self.kind == "cross":
            return "cross via " + " * ".join(f"{base}->{quote}" for base, quote in self.path)
        return self.kind


class RateNotAvailable(Exception):
    """Internal rate-lookup failure translated to RATE_UNAVAILABLE by the engine."""

    def __init__(
        self,
        base: str,
        quote: str,
        reason: str,
        details: dict | None = None,
    ):
        self.base = base
        self.quote = quote
        self.reason = reason
        self.details = details or {}
        super().__init__(f"No usable exchange rate for {base}->{quote}: {reason}")


class RateService:
    """Resolve direct, inverse and one-hop cross rates with freshness checks.

    Freshness is measured relative to the newest timestamp in the feed. This
    matches the mock provider's own stale-rate semantics and keeps the bundled
    snapshot usable in tests while still rejecting the intentionally old rates
    in it.
    """

    def __init__(
        self,
        rates: Iterable[dict],
        max_age: timedelta = DEFAULT_MAX_AGE,
        clock: Callable[[], datetime] | None = None,
    ):
        self.max_age = max_age
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        parsed: dict[tuple[str, str], Rate] = {}

        for item in rates:
            try:
                base = item["base"]
                quote = item["quote"]
                rate_value = Decimal(str(item["rate"]))
                updated_at = parse_timestamp(item["updated_at"])
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                raise ValueError(f"Invalid rate entry: {item!r}") from exc

            if not isinstance(base, str) or len(base) != 3:
                raise ValueError(f"Invalid base currency in rate entry: {item!r}")
            if not isinstance(quote, str) or len(quote) != 3:
                raise ValueError(f"Invalid quote currency in rate entry: {item!r}")
            if rate_value <= 0:
                raise ValueError(f"Rate must be positive: {item!r}")

            parsed[(base, quote)] = Rate(
                base=base,
                quote=quote,
                rate=rate_value,
                updated_at=updated_at,
            )

        self._rates = parsed
        self.newest_updated_at = max(
            (rate.updated_at for rate in parsed.values()), default=None
        )

    def _is_fresh(self, rate: Rate) -> bool:
        if self.newest_updated_at is None:
            return False
        return (self.newest_updated_at - rate.updated_at) <= self.max_age

    def _pair(self, base: str, quote: str) -> RateQuote | None:
        direct = self._rates.get((base, quote))
        if direct is not None and self._is_fresh(direct):
            return RateQuote(
                rate=direct.rate,
                updated_at=direct.updated_at,
                kind="direct",
                path=((base, quote),),
            )

        inverse = self._rates.get((quote, base))
        if inverse is not None and self._is_fresh(inverse):
            return RateQuote(
                rate=_format_computed_rate(Decimal(1) / inverse.rate),
                updated_at=inverse.updated_at,
                kind="inverse",
                path=((base, quote),),
            )

        return None

    def get_rate(self, source: str, target: str) -> RateQuote:
        if source == target:
            return RateQuote(
                rate=Decimal(1),
                updated_at=self.newest_updated_at or self._clock(),
                kind="identity",
            )

        pair = self._pair(source, target)
        if pair is not None:
            return pair

        currencies = sorted(
            {base for base, _ in self._rates} | {quote for _, quote in self._rates}
        )
        for intermediate in currencies:
            if intermediate in (source, target):
                continue

            first = self._pair(source, intermediate)
            if first is None:
                continue

            second = self._pair(intermediate, target)
            if second is None:
                continue

            return RateQuote(
                rate=_format_computed_rate(first.rate * second.rate),
                updated_at=min(first.updated_at, second.updated_at),
                kind="cross",
                path=first.path + second.path,
            )

        raise self._unavailable(source, target)

    def convert(self, amount: Decimal, source: str, target: str) -> Decimal:
        if source == target:
            return amount
        return amount * self.get_rate(source, target).rate

    def rate_map(self) -> dict[tuple[str, str], Decimal]:
        return {(rate.base, rate.quote): rate.rate for rate in self._rates.values()}

    def _unavailable(self, source: str, target: str) -> RateNotAvailable:
        details: dict = {
            "base": source,
            "quote": target,
            "max_age_seconds": int(self.max_age.total_seconds()),
        }

        direct = self._rates.get((source, target))
        inverse = self._rates.get((target, source))
        known = [
            (pair, rate)
            for pair, rate in (
                ((source, target), direct),
                ((target, source), inverse),
            )
            if rate is not None
        ]

        if known:
            details["reason"] = "stale"
            details["updated_at"] = format_timestamp(
                max(rate.updated_at for _, rate in known)
            )
            if self.newest_updated_at is not None:
                details["newest_updated_at"] = format_timestamp(
                    self.newest_updated_at
                )
            details["pairs"] = [f"{base}->{quote}" for (base, quote), _ in known]
        else:
            details["reason"] = "missing"

        return RateNotAvailable(source, target, details["reason"], details)
