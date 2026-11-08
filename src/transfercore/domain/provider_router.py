from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Mapping

from transfercore.domain.rates import RateNotAvailable, RateService


@dataclass(frozen=True)
class ProviderEvaluation:
    provider_id: str
    score: float
    estimated_fee_source: Decimal
    margin_percent: Decimal
    is_margin_compliant: bool
    provider_currency: str = ""
    target_in_provider_currency: Decimal = Decimal("0")
    margin_amount_source: Decimal = Decimal("0")


class CurrencyConverter:
    """Helper to convert between currencies using a raw rates table."""

    def __init__(self, rates_map: Mapping[tuple[str, str], Decimal]):
        self.rates = dict(rates_map)

    def convert(self, amount: Decimal, source: str, target: str) -> Decimal | None:
        if source == target:
            return amount
        # Direct
        if (source, target) in self.rates:
            return amount * self.rates[(source, target)]
        # Inverse
        if (target, source) in self.rates:
            return amount / self.rates[(target, source)]
        # Cross-rate via intermediary
        for (b, q), r in self.rates.items():
            if b == source and (q, target) in self.rates:
                return amount * r * self.rates[(q, target)]
        return None


class ProviderRouter:
    """
    Filters and ranks providers for a transfer based on:
    1. Corridor compatibility (direction_id)
    2. Active maintenance windows
    3. Payout amount limits (in provider currency)
    4. Minimum margin requirement (strict, no below-minimum fallback)
    5. Scoring formula combining success_rate, cost/margin, and latency
    """

    # Scoring weights are documented in DECISIONS.md.
    SUCCESS_WEIGHT = 0.5
    MARGIN_WEIGHT = 0.3
    LATENCY_WEIGHT = 0.2
    LATENCY_CEILING_MS = 5000.0
    MARGIN_SCORE_CEILING_PERCENT = Decimal("5")

    def __init__(
        self,
        providers: list[dict],
        rates_map: Mapping[tuple[str, str], Decimal] | None = None,
        rate_service: RateService | None = None,
    ):
        self.providers = providers
        self.rate_service = rate_service
        self.converter = CurrencyConverter(rates_map or {})

    def _convert(
        self, amount: Decimal, source: str, target: str
    ) -> Decimal | None:
        if source == target:
            return amount
        if self.rate_service is not None:
            try:
                return self.rate_service.convert(amount, source, target)
            except RateNotAvailable:
                return None
        return self.converter.convert(amount, source, target)

    def select_providers(
        self,
        direction_id: str,
        target_amount: Decimal,
        target_currency: str,
        source_amount: Decimal,
        source_currency: str,
        client_fee: Decimal,
        min_margin_percent: Decimal,
        now: datetime | None = None,
        client_fee_currency: str | None = None,
    ) -> list[str]:
        """
        Return eligible provider IDs ordered by score (best first).

        A provider is eligible only when it is enabled, supports the direction,
        is not in maintenance, accepts the payout amount in its own currency,
        and its margin is at least ``min_margin_percent``. There is no silent
        fallback to a provider below the minimum margin.
        """
        current_time = now or datetime.now(timezone.utc)
        fee_currency = client_fee_currency or source_currency
        client_fee_source = self._convert(
            client_fee, fee_currency, source_currency
        )
        if client_fee_source is None:
            return []

        evaluated: list[ProviderEvaluation] = []

        for p in self.providers:
            if not p.get("enabled", True):
                continue

            # 1. Direction support
            if direction_id not in p.get("directions", []):
                continue

            # 2. Maintenance window check
            if self._in_maintenance(p, current_time):
                continue

            # 3. Provider limits check (in provider currency)
            prov_currency = p.get("currency", "USD")
            target_in_prov = self._convert(
                target_amount, target_currency, prov_currency
            )
            if target_in_prov is None:
                continue

            limits = p.get("limits", {})
            min_lim = Decimal(str(limits.get("min", "0")))
            max_lim = Decimal(str(limits.get("max", "999999999")))
            if not (min_lim <= target_in_prov <= max_lim):
                continue

            # 4. Provider fee calculation
            fee_cfg = p.get("fee", {})
            p_percent = Decimal(str(fee_cfg.get("percent", "0"))) / Decimal("100")
            p_fixed = Decimal(str(fee_cfg.get("fixed", "0")))
            prov_fee_in_prov = p_fixed + target_in_prov * p_percent

            # Convert provider fee to source currency
            prov_fee_source = self._convert(
                prov_fee_in_prov, prov_currency, source_currency
            )
            if prov_fee_source is None:
                continue

            # 5. Margin check
            margin_amount = client_fee_source - prov_fee_source
            margin_percent = (
                (margin_amount / source_amount) * Decimal("100")
                if source_amount > 0
                else Decimal("0")
            )
            is_compliant = margin_percent >= min_margin_percent

            # 6. Scoring: success rate, margin and latency.
            sr = float(p.get("success_rate_24h", "0.95"))
            latency_ms = float(p.get("p95_latency_ms", "2000"))
            latency_score = max(
                0.0, 1.0 - (latency_ms / self.LATENCY_CEILING_MS)
            )
            margin_score = float(
                max(
                    Decimal("0"),
                    min(
                        Decimal("1"),
                        margin_percent / self.MARGIN_SCORE_CEILING_PERCENT,
                    ),
                )
            )

            score = (
                self.SUCCESS_WEIGHT * sr
                + self.MARGIN_WEIGHT * margin_score
                + self.LATENCY_WEIGHT * latency_score
            )

            evaluated.append(
                ProviderEvaluation(
                    provider_id=p["provider_id"],
                    score=score,
                    estimated_fee_source=prov_fee_source,
                    margin_percent=margin_percent,
                    is_margin_compliant=is_compliant,
                    provider_currency=prov_currency,
                    target_in_provider_currency=target_in_prov,
                    margin_amount_source=margin_amount,
                )
            )

        # Strict margin: a provider below min_margin_percent is never used.
        compliant = [e for e in evaluated if e.is_margin_compliant]
        compliant.sort(key=lambda e: (-e.score, e.provider_id))
        return [e.provider_id for e in compliant]

    def evaluate(
        self,
        direction_id: str,
        target_amount: Decimal,
        target_currency: str,
        source_amount: Decimal,
        source_currency: str,
        client_fee: Decimal,
        min_margin_percent: Decimal,
        now: datetime | None = None,
        client_fee_currency: str | None = None,
    ) -> list[ProviderEvaluation]:
        """Return full evaluations, including rejected providers, for audits."""
        current_time = now or datetime.now(timezone.utc)
        fee_currency = client_fee_currency or source_currency
        client_fee_source = self._convert(
            client_fee, fee_currency, source_currency
        )
        if client_fee_source is None:
            return []

        results: list[ProviderEvaluation] = []
        for p in self.providers:
            prov_currency = p.get("currency", "USD")
            target_in_prov = self._convert(
                target_amount, target_currency, prov_currency
            )
            limits = p.get("limits", {})
            min_lim = Decimal(str(limits.get("min", "0")))
            max_lim = Decimal(str(limits.get("max", "999999999")))
            fee_cfg = p.get("fee", {})
            p_percent = Decimal(str(fee_cfg.get("percent", "0"))) / Decimal("100")
            p_fixed = Decimal(str(fee_cfg.get("fixed", "0")))
            prov_fee_in_prov = (
                p_fixed + target_in_prov * p_percent
                if target_in_prov is not None
                else Decimal("0")
            )
            prov_fee_source = self._convert(
                prov_fee_in_prov, prov_currency, source_currency
            )
            margin_amount = (
                client_fee_source - prov_fee_source
                if prov_fee_source is not None
                else Decimal("0")
            )
            margin_percent = (
                (margin_amount / source_amount) * Decimal("100")
                if source_amount > 0
                else Decimal("0")
            )
            sr = float(p.get("success_rate_24h", "0.95"))
            latency_ms = float(p.get("p95_latency_ms", "2000"))
            latency_score = max(
                0.0, 1.0 - (latency_ms / self.LATENCY_CEILING_MS)
            )
            margin_score = float(
                max(
                    Decimal("0"),
                    min(
                        Decimal("1"),
                        margin_percent / self.MARGIN_SCORE_CEILING_PERCENT,
                    ),
                )
            )
            score = (
                self.SUCCESS_WEIGHT * sr
                + self.MARGIN_WEIGHT * margin_score
                + self.LATENCY_WEIGHT * latency_score
            )
            eligible = (
                p.get("enabled", True)
                and direction_id in p.get("directions", [])
                and not self._in_maintenance(p, current_time)
                and target_in_prov is not None
                and min_lim <= target_in_prov <= max_lim
                and prov_fee_source is not None
            )
            results.append(
                ProviderEvaluation(
                    provider_id=p["provider_id"],
                    score=score,
                    estimated_fee_source=prov_fee_source or Decimal("0"),
                    margin_percent=margin_percent,
                    is_margin_compliant=(
                        eligible and margin_percent >= min_margin_percent
                    ),
                    provider_currency=prov_currency,
                    target_in_provider_currency=target_in_prov or Decimal("0"),
                    margin_amount_source=margin_amount,
                )
            )
        return results

    @staticmethod
    def _in_maintenance(provider: dict, current_time: datetime) -> bool:
        for window in provider.get("maintenance_windows", []):
            try:
                from_dt = datetime.fromisoformat(
                    window["from"].replace("Z", "+00:00")
                )
                to_dt = datetime.fromisoformat(window["to"].replace("Z", "+00:00"))
                if from_dt <= current_time < to_dt:
                    return True
            except Exception:
                continue
        return False
