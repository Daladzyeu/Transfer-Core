import asyncio
import logging
import os
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from transfercore.domain.money import CurrencyRegistry, Money, registry
from transfercore.domain.provider_router import ProviderRouter
from transfercore.domain.rates import RateService
from transfercore.infrastructure.db import AsyncSessionLocal
from transfercore.infrastructure.provider_client import (
    MockProviderClient,
    PayoutStatus,
)
from transfercore.infrastructure.repositories import (
    BalanceRepository,
    TransferRepository,
)
from transfercore.infrastructure.tables import Transfers, TransferState

log = logging.getLogger(__name__)

TERMINAL_STATES = (TransferState.COMPLETED, TransferState.FAILED)
PROVIDER_STATUSES = {"PROCESSING", "COMPLETED", "FAILED"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _append_transition(
    transfer: Transfers,
    new_status: TransferState,
    *,
    source: str,
    reason: str | None = None,
    at: datetime | None = None,
) -> None:
    transitions = list(transfer.transitions or [])
    transition = {
        "from": transfer.status.value if transfer.status else None,
        "to": new_status.value,
        "timestamp": (at or _now()).isoformat(),
        "source": source,
    }
    if reason:
        transition["reason"] = reason
    transitions.append(transition)
    transfer.transitions = transitions


def _review_started_at(transfer: Transfers) -> datetime | None:
    """Return when the transfer last entered NEEDS_REVIEW, if known."""
    for transition in reversed(transfer.transitions or []):
        if transition.get("to") != TransferState.NEEDS_REVIEW.value:
            continue
        timestamp = transition.get("timestamp")
        if not timestamp:
            return None
        try:
            return datetime.fromisoformat(timestamp)
        except ValueError:
            return None
    return None


def _merge_attempts(
    existing: list[dict] | None, new_attempts: list[dict]
) -> list[dict]:
    merged: dict[str, dict] = {}
    for attempt in list(existing or []) + list(new_attempts):
        key = attempt.get("attempt_key") or (
            f"{attempt.get('provider_id')}:{attempt.get('timestamp')}"
        )
        merged[key] = attempt
    return list(merged.values())


def _attempt(
    transfer_id: str,
    provider_id: str,
    response,
    *,
    status: str | None = None,
) -> dict:
    return {
        "attempt_key": f"{transfer_id}-{provider_id}",
        "provider_id": provider_id,
        "payout_id": getattr(response, "payout_id", None),
        "status": status or getattr(response, "status", "ERROR"),
        "error_code": getattr(response, "error_code", None),
        "error_message": getattr(response, "error_message", None),
        "http_status": getattr(response, "http_status", 0),
        "timestamp": _now().isoformat(),
    }


async def record_attempt_only(
    session: AsyncSession,
    transfer_id: str,
    attempts: list[dict],
) -> Transfers | None:
    transfer = await TransferRepository.get_by_id_for_update(session, transfer_id)
    if transfer is None:
        return None
    transfer.attempts = _merge_attempts(transfer.attempts, attempts)
    transfer.claimed_at = None
    session.add(transfer)
    await session.commit()
    return transfer


async def mark_processing(
    session: AsyncSession,
    transfer_id: str,
    provider_id: str,
    payout_id: str | None,
    attempts: list[dict],
    *,
    source: str = "worker",
) -> Transfers | None:
    transfer = await TransferRepository.get_by_id_for_update(session, transfer_id)
    if transfer is None:
        return None
    if transfer.status in TERMINAL_STATES:
        return await record_attempt_only(session, transfer_id, attempts)

    if transfer.status != TransferState.PROCESSING:
        _append_transition(
            transfer,
            TransferState.PROCESSING,
            source=source,
            reason=f"provider {provider_id} accepted the payout",
        )
    transfer.status = TransferState.PROCESSING
    transfer.provider_id = provider_id
    transfer.provider_payout_id = payout_id
    transfer.failure_code = None
    transfer.review_reason = None
    transfer.claimed_at = None
    transfer.attempts = _merge_attempts(transfer.attempts, attempts)
    session.add(transfer)
    await session.commit()
    return transfer


async def mark_needs_review(
    session: AsyncSession,
    transfer_id: str,
    provider_id: str | None,
    attempts: list[dict],
    reason: str,
    *,
    failure_code: str = "PROVIDER_OUTCOME_UNKNOWN",
) -> Transfers | None:
    transfer = await TransferRepository.get_by_id_for_update(session, transfer_id)
    if transfer is None:
        return None
    if transfer.status in TERMINAL_STATES:
        return await record_attempt_only(session, transfer_id, attempts)

    if transfer.status != TransferState.NEEDS_REVIEW:
        _append_transition(
            transfer,
            TransferState.NEEDS_REVIEW,
            source="worker",
            reason=reason,
        )
    transfer.status = TransferState.NEEDS_REVIEW
    if provider_id:
        transfer.provider_id = provider_id
    transfer.review_reason = reason
    transfer.failure_code = failure_code
    transfer.claimed_at = None
    transfer.attempts = _merge_attempts(transfer.attempts, attempts)
    session.add(transfer)
    await session.commit()
    return transfer


async def fail_transfer(
    session: AsyncSession,
    transfer_id: str,
    attempts: list[dict],
    failure_code: str,
    reason: str,
    *,
    source: str = "worker",
) -> Transfers | None:
    transfer = await TransferRepository.get_by_id_for_update(session, transfer_id)
    if transfer is None:
        return None
    if transfer.status in TERMINAL_STATES:
        return await record_attempt_only(session, transfer_id, attempts)

    await BalanceRepository.release_hold(
        session,
        transfer.client_id,
        transfer.source_currency,
        Decimal(str(transfer.source_amount)),
    )
    _append_transition(
        transfer,
        TransferState.FAILED,
        source=source,
        reason=reason,
    )
    transfer.status = TransferState.FAILED
    transfer.failure_code = failure_code
    transfer.review_reason = reason
    transfer.claimed_at = None
    transfer.attempts = _merge_attempts(transfer.attempts, attempts)
    session.add(transfer)
    await session.commit()
    return transfer


async def apply_provider_status(
    session: AsyncSession,
    transfer_id: str,
    provider_status: str,
    *,
    provider_id: str | None = None,
    payout_id: str | None = None,
    failure_code: str | None = None,
    occurred_at: datetime | None = None,
    source: str = "provider",
    reason: str | None = None,
) -> tuple[Transfers | None, bool]:
    """Apply a provider/webhook/reconciliation status exactly once.

    Terminal states are sticky: a later contradictory event is ignored.
    Events older than the last accepted event are ignored as out of order.
    """
    transfer = await TransferRepository.get_by_id_for_update(session, transfer_id)
    if transfer is None:
        return None, False

    status = (provider_status or "").upper()
    if status not in PROVIDER_STATUSES:
        return transfer, False

    if transfer.status in TERMINAL_STATES:
        return transfer, False

    if occurred_at is not None:
        if transfer.last_event_at is not None and occurred_at <= transfer.last_event_at:
            return transfer, False
        transfer.last_event_at = occurred_at

    if provider_id and not transfer.provider_id:
        transfer.provider_id = provider_id
    if payout_id and not transfer.provider_payout_id:
        transfer.provider_payout_id = payout_id

    if status == "PROCESSING":
        if transfer.status != TransferState.PROCESSING:
            _append_transition(
                transfer,
                TransferState.PROCESSING,
                source=source,
                reason=reason or "provider reports processing",
                at=occurred_at,
            )
            transfer.status = TransferState.PROCESSING
        transfer.review_reason = None
        transfer.failure_code = None
        transfer.claimed_at = None
        session.add(transfer)
        await session.commit()
        return transfer, True

    if status == "COMPLETED":
        await BalanceRepository.debit_held_funds(
            session,
            transfer.client_id,
            transfer.source_currency,
            Decimal(str(transfer.source_amount)),
        )
        _append_transition(
            transfer,
            TransferState.COMPLETED,
            source=source,
            reason=reason or "provider completed the payout",
            at=occurred_at,
        )
        transfer.status = TransferState.COMPLETED
        transfer.failure_code = None
        transfer.review_reason = None
        transfer.claimed_at = None
        session.add(transfer)
        await session.commit()
        return transfer, True

    # FAILED
    await BalanceRepository.release_hold(
        session,
        transfer.client_id,
        transfer.source_currency,
        Decimal(str(transfer.source_amount)),
    )
    _append_transition(
        transfer,
        TransferState.FAILED,
        source=source,
        reason=reason or "provider failed the payout",
        at=occurred_at,
    )
    transfer.status = TransferState.FAILED
    transfer.failure_code = failure_code or "PROVIDER_FAILED"
    transfer.review_reason = None
    transfer.claimed_at = None
    session.add(transfer)
    await session.commit()
    return transfer, True


async def _update_reconciled_at(
    session: AsyncSession, transfer_id: str
) -> None:
    transfer = await TransferRepository.get_by_id_for_update(session, transfer_id)
    if transfer is None:
        return
    transfer.last_reconciled_at = _now()
    session.add(transfer)
    await session.commit()


class TransferWorker:
    """Background dispatcher and reconciler for transfers.

    Transfers stay ``PENDING`` until a provider accepts a payout. This makes a
    worker crash safe: the provider idempotency key is stable, so a retry
    replays the existing payout instead of creating a second one.
    """

    def __init__(
        self,
        state,
        provider_client=None,
        *,
        poll_interval: float | None = None,
        reconcile_interval: float | None = None,
        reconcile_min_age: float | None = None,
        claim_timeout: float | None = None,
        review_grace: float | None = None,
        concurrency: int | None = None,
        batch_size: int | None = None,
    ):
        self.state = state
        self.provider_client = provider_client or MockProviderClient()
        self.registry: CurrencyRegistry = getattr(
            state, "currency_registry", registry
        )
        self.rate_service: RateService | None = getattr(
            state, "rate_service", None
        )
        self.providers = getattr(state, "providers", [])
        self.clients = getattr(state, "clients", {})
        self.tariffs = {
            tariff.direction_id: tariff
            for tariff in getattr(
                getattr(state, "quote_engine", None), "tariffs", []
            )
        }

        self.poll_interval = (
            poll_interval
            if poll_interval is not None
            else float(os.environ.get("TRANSFER_WORKER_POLL_SEC", "0.5"))
        )
        self.reconcile_interval = (
            reconcile_interval
            if reconcile_interval is not None
            else float(os.environ.get("TRANSFER_RECONCILE_SEC", "2.0"))
        )
        self.reconcile_min_age = (
            reconcile_min_age
            if reconcile_min_age is not None
            else float(os.environ.get("TRANSFER_RECONCILE_MIN_AGE_SEC", "1.0"))
        )
        self.claim_timeout = timedelta(
            seconds=(
                claim_timeout
                if claim_timeout is not None
                else float(os.environ.get("TRANSFER_CLAIM_TIMEOUT_SEC", "120"))
            )
        )
        self.review_grace = (
            review_grace
            if review_grace is not None
            else float(os.environ.get("TRANSFER_REVIEW_GRACE_SEC", "30"))
        )
        self.concurrency = (
            concurrency
            if concurrency is not None
            else int(os.environ.get("TRANSFER_WORKER_CONCURRENCY", "5"))
        )
        self.batch_size = (
            batch_size
            if batch_size is not None
            else int(os.environ.get("TRANSFER_WORKER_BATCH", "10"))
        )
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_reconcile = 0.0
        self._semaphore = asyncio.Semaphore(self.concurrency)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(), name="transfer-worker"
            )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.process_pending()
            except Exception:
                log.exception("transfer worker: pending dispatch failed")

            now = asyncio.get_running_loop().time()
            if now - self._last_reconcile >= self.reconcile_interval:
                self._last_reconcile = now
                try:
                    await self.reconcile()
                except Exception:
                    log.exception("transfer worker: reconciliation failed")

            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.poll_interval
                )
            except asyncio.TimeoutError:
                pass

    async def process_pending(self) -> int:
        async with AsyncSessionLocal() as session:
            transfers = await TransferRepository.claim_pending(
                session, self.batch_size, self.claim_timeout
            )
        if not transfers:
            return 0

        async def run(transfer_id: str) -> None:
            async with self._semaphore:
                try:
                    await self.dispatch(transfer_id)
                except Exception:
                    log.exception(
                        "transfer worker: dispatch failed for %s", transfer_id
                    )

        await asyncio.gather(*(run(t.transfer_id) for t in transfers))
        return len(transfers)

    async def dispatch(self, transfer_id: str) -> None:
        async with AsyncSessionLocal() as session:
            transfer = await TransferRepository.get_by_id(session, transfer_id)
            if transfer is None or transfer.status != TransferState.PENDING:
                return

            direction_id = transfer.direction_id
            source_amount = Decimal(str(transfer.source_amount))
            source_currency = transfer.source_currency
            target_amount = Decimal(str(transfer.target_amount))
            target_currency = transfer.target_currency
            client_fee = Decimal(str(transfer.fee_amount or 0))
            fee_currency = transfer.fee_currency or source_currency
            tariff = self.tariffs.get(direction_id)
            min_margin = (
                tariff.min_margin_percent if tariff else Decimal("0")
            )

            router = ProviderRouter(
                self.providers, rate_service=self.rate_service
            )
            ranked = router.select_providers(
                direction_id=direction_id,
                target_amount=target_amount,
                target_currency=target_currency,
                source_amount=source_amount,
                source_currency=source_currency,
                client_fee=client_fee,
                min_margin_percent=min_margin,
                client_fee_currency=fee_currency,
            )

            if not ranked:
                await fail_transfer(
                    session,
                    transfer_id,
                    [],
                    "NO_ELIGIBLE_PROVIDER",
                    "no provider satisfies direction, limits and minimum margin",
                )
                return

            attempts: list[dict] = []
            target_text = Money.from_decimal(
                target_amount, target_currency, self.registry
            ).to_str(self.registry)

            for provider_id in ranked:
                attempt_key = f"{transfer_id}-{provider_id}"
                response = await self.provider_client.create_payout(
                    provider_id=provider_id,
                    idempotency_key=attempt_key,
                    reference=transfer_id,
                    amount=target_text,
                    currency=target_currency,
                    recipient=transfer.recipient or {},
                )
                attempts.append(_attempt(transfer_id, provider_id, response))

                if response.is_success:
                    current = await TransferRepository.get_by_id(
                        session, transfer_id
                    )
                    if current and current.status in TERMINAL_STATES:
                        await record_attempt_only(
                            session, transfer_id, attempts
                        )
                        return
                    if response.status == "COMPLETED":
                        await record_attempt_only(
                            session, transfer_id, attempts
                        )
                        await apply_provider_status(
                            session,
                            transfer_id,
                            "COMPLETED",
                            provider_id=provider_id,
                            payout_id=response.payout_id,
                            source="provider",
                            reason="provider accepted and completed the payout",
                        )
                    else:
                        await mark_processing(
                            session,
                            transfer_id,
                            provider_id,
                            response.payout_id,
                            attempts,
                        )
                    return

                if response.status == "DECLINED":
                    continue

                # A 5xx, timeout or transport failure is ambiguous: the
                # provider may have created the payout even though we did not
                # receive a successful response. Never try another provider
                # here; that could pay the recipient twice. Look the payout up
                # and, if its state is still unknown, park the transfer for
                # reconciliation.
                lookup: PayoutStatus = await self.provider_client.find_payout(
                    provider_id, transfer_id
                )
                if lookup.found:
                    if lookup.status in ("COMPLETED", "FAILED"):
                        await apply_provider_status(
                            session,
                            transfer_id,
                            lookup.status,
                            provider_id=provider_id,
                            payout_id=lookup.payout_id
                            or response.payout_id,
                            failure_code=lookup.failure_code,
                            source="provider",
                            reason="resolved after ambiguous provider response",
                        )
                    else:
                        await mark_processing(
                            session,
                            transfer_id,
                            provider_id,
                            lookup.payout_id or response.payout_id,
                            attempts,
                        )
                    return

                # The payout is not visible (or the lookup failed). The
                # provider read model may lag behind its writes, so absence is
                # not proof that no payout exists. Hold the funds and let the
                # reconciliation loop resolve the transfer.
                await mark_needs_review(
                    session,
                    transfer_id,
                    provider_id,
                    attempts,
                    "provider outcome is unknown; reconciliation required",
                    failure_code=response.error_code or "PROVIDER_UNKNOWN",
                )
                return

            declined_only = bool(attempts) and all(
                attempt.get("status") == "DECLINED" for attempt in attempts
            )
            await fail_transfer(
                session,
                transfer_id,
                attempts,
                "PROVIDER_DECLINED" if declined_only else "PROVIDER_ERROR",
                (
                    "all eligible providers declined the payout"
                    if declined_only
                    else "all eligible providers failed without creating a payout"
                ),
            )

    async def reconcile(self) -> int:
        async with AsyncSessionLocal() as session:
            transfers = await TransferRepository.get_reconcilable(
                session,
                self.batch_size,
                timedelta(seconds=self.reconcile_min_age),
            )
        for transfer in transfers:
            try:
                await self._reconcile_one(transfer.transfer_id)
            except Exception:
                log.exception(
                    "transfer worker: reconciliation failed for %s",
                    transfer.transfer_id,
                )
        return len(transfers)

    async def _reconcile_one(self, transfer_id: str) -> None:
        async with AsyncSessionLocal() as session:
            transfer = await TransferRepository.get_by_id(session, transfer_id)
            if transfer is None or transfer.status not in (
                TransferState.PROCESSING,
                TransferState.NEEDS_REVIEW,
            ):
                return
            provider_id = transfer.provider_id
            if not provider_id:
                return

            review_started = None
            if transfer.status == TransferState.NEEDS_REVIEW:
                review_started = _review_started_at(transfer)
                if review_started is not None:
                    age = (_now() - review_started).total_seconds()
                    if age < self.review_grace:
                        # A provider's read model can lag behind its writes.
                        # Do not conclude anything until the grace expires.
                        await _update_reconciled_at(session, transfer_id)
                        return

            if transfer.provider_payout_id:
                status = await self.provider_client.get_payout(
                    provider_id, transfer.provider_payout_id
                )
            else:
                status = await self.provider_client.find_payout(
                    provider_id, transfer.transfer_id
                )

            if status.found:
                if status.status in ("COMPLETED", "FAILED"):
                    await apply_provider_status(
                        session,
                        transfer_id,
                        status.status,
                        provider_id=provider_id,
                        payout_id=status.payout_id
                        or transfer.provider_payout_id,
                        failure_code=status.failure_code,
                        source="reconciliation",
                        reason="reconciled provider status",
                    )
                    return
                if (
                    status.status == "PROCESSING"
                    and transfer.status == TransferState.NEEDS_REVIEW
                ):
                    # The payout exists, so the outcome is no longer unknown.
                    await apply_provider_status(
                        session,
                        transfer_id,
                        "PROCESSING",
                        provider_id=provider_id,
                        payout_id=status.payout_id
                        or transfer.provider_payout_id,
                        source="reconciliation",
                        reason="provider reports a live payout",
                    )
                    return
            elif (
                status.is_not_found
                and transfer.status == TransferState.NEEDS_REVIEW
                and review_started is not None
            ):
                # After the review grace a definitive 404 means the provider
                # never created a payout. Release the hold instead of leaving
                # the client's money reserved forever.
                await fail_transfer(
                    session,
                    transfer_id,
                    [],
                    "PROVIDER_ERROR",
                    "provider has no payout after review grace",
                    source="reconciliation",
                )
                return

            await _update_reconciled_at(session, transfer_id)
