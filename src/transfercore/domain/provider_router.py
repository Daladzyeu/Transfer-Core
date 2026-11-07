from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Mapping


@dataclass(frozen=True)
class ProviderEvaluation:
    provider_id: str
    score: float
    estimated_fee_source: Decimal
    margin_percent: Decimal
    is_margin_compliant: bool


class CurrencyConverter:
    """Helper to convert between currencies using the rates table."""

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
    4. Minimum margin requirement
    5. Scoring formula combining success_rate, cost, and latency
    """

    def __init__(
        self,
        providers: list[dict],
        rates_map: Mapping[tuple[str, str], Decimal],
    ):
        self.providers = providers
        self.converter = CurrencyConverter(rates_map)

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
    ) -> list[str]:
        """
        Returns an ordered list of eligible provider IDs (primary first, then fallbacks).
        """
        current_time = now or datetime.now(timezone.utc)
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
            target_in_prov = self.converter.convert(
                target_amount, target_currency, prov_currency
            )
            if target_in_prov is None:
                # If cannot convert, fallback to allowing if cannot verify
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
            prov_fee_source = self.converter.convert(
                prov_fee_in_prov, prov_currency, source_currency
            )
            if prov_fee_source is None:
                prov_fee_source = Decimal("0")

            # 5. Margin check
            margin_amount = client_fee - prov_fee_source
            margin_percent = (
                (margin_amount / source_amount) * Decimal("100")
                if source_amount > 0
                else Decimal("0")
            )
            is_compliant = margin_percent >= min_margin_percent

            # 6. Scoring: weights = 0.5 success_rate, 0.3 cost/margin, 0.2 latency
            sr = float(p.get("success_rate_24h", "0.95"))
            latency_ms = float(p.get("p95_latency_ms", "2000"))
            latency_score = max(0.0, 1.0 - (latency_ms / 5000.0))
            margin_score = float(
                max(Decimal("0"), min(Decimal("1"), margin_percent / Decimal("5")))
            )

            score = (0.5 * sr) + (0.3 * margin_score) + (0.2 * latency_score)

            evaluated.append(
                ProviderEvaluation(
                    provider_id=p["provider_id"],
                    score=score,
                    estimated_fee_source=prov_fee_source,
                    margin_percent=margin_percent,
                    is_margin_compliant=is_compliant,
                )
            )

        # Primary selection: compliant margin providers first, sorted by score descending
        compliant = [e for e in evaluated if e.is_margin_compliant]
        non_compliant = [e for e in evaluated if not e.is_margin_compliant]

        compliant.sort(key=lambda x: x.score, reverse=True)
        # We also allow non-compliant as emergency fallback (documented in DECISIONS.md)
        non_compliant.sort(key=lambda x: x.score, reverse=True)

        return [e.provider_id for e in compliant + non_compliant]

    @staticmethod
    def _in_maintenance(provider: dict, current_time: datetime) -> bool:
        for window in provider.get("maintenance_windows", []):
            try:
                from_dt = datetime.fromisoformat(window["from"].replace("Z", "+00:00"))
                to_dt = datetime.fromisoformat(window["to"].replace("Z", "+00:00"))
                if from_dt <= current_time < to_dt:
                    return True
            except Exception:
                continue
        return False
