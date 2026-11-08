import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from transfercore.api.app import create_app
from transfercore.infrastructure.db import AsyncSessionLocal
from transfercore.infrastructure.repositories import (
    BalanceRepository,
    TransferRepository,
)
from transfercore.infrastructure.tables import Transfers, TransferState
from transfercore.infrastructure.transfer_worker import (
    TransferWorker,
    apply_provider_status,
)

from fakes import (
    FakeProviderClient,
    accepted,
    declined,
    found,
    lookup_failed,
    missing,
    provider_error,
    timed_out,
)

START_BALANCE = Decimal("500000.00")
TRANSFER_AMOUNT = Decimal("5000.00")

QUOTE_REQUEST = {
    "source_country": "RU",
    "source_currency": "RUB",
    "target_country": "TH",
    "target_currency": "THB",
    "payout_method": "BANK_ACCOUNT",
    "amount": "5000.00",
    "amount_type": "SOURCE",
}

RECIPIENT = {
    "name": "Somchai Jaidee",
    "account": "1234567890",
    "bank_code": "BKKB",
}


@asynccontextmanager
async def transfer_app():
    app = create_app(start_worker=False)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield app, client


async def create_quote(client: AsyncClient, amount: str = "5000.00") -> str:
    response = await client.post("/quotes", json={**QUOTE_REQUEST, "amount": amount})
    assert response.status_code == 201, response.text
    return response.json()["quote_id"]


async def create_transfer(
    client: AsyncClient,
    quote_id: str,
    *,
    key: str | None = None,
    client_id: str = "c_001",
    recipient: dict | None = None,
) -> tuple[int, dict]:
    response = await client.post(
        "/transfers",
        json={
            "quote_id": quote_id,
            "client_id": client_id,
            "recipient": recipient or RECIPIENT,
        },
        headers={"Idempotency-Key": key or f"key-{uuid.uuid4().hex}"},
    )
    return response.status_code, response.json()


async def balance_of(client_id: str = "c_001", currency: str = "RUB"):
    async with AsyncSessionLocal() as session:
        balance = await BalanceRepository.get_balance_for_update(
            session, client_id, currency
        )
        assert balance is not None
        return Decimal(str(balance.available)), Decimal(str(balance.reserved))


async def transfer_row(transfer_id: str) -> Transfers:
    async with AsyncSessionLocal() as session:
        transfer = await TransferRepository.get_by_id(session, transfer_id)
        assert transfer is not None
        return transfer


async def completed_total(client_id: str = "c_001", currency: str = "RUB") -> Decimal:
    async with AsyncSessionLocal() as session:
        total = await session.scalar(
            select(func.coalesce(func.sum(Transfers.source_amount), 0)).where(
                Transfers.client_id == client_id,
                Transfers.source_currency == currency,
                Transfers.status == TransferState.COMPLETED,
            )
        )
        return Decimal(str(total or 0))


async def count_transfers(client_id: str = "c_001") -> int:
    async with AsyncSessionLocal() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(Transfers)
                .where(Transfers.client_id == client_id)
            )
        )


async def assert_funds_invariant(
    starting: Decimal = START_BALANCE, client_id: str = "c_001"
) -> None:
    available, reserved = await balance_of(client_id)
    debited = await completed_total(client_id)
    assert available + reserved + debited == starting


async def insert_historical_transfer(
    amount: Decimal,
    created_at: datetime,
    *,
    client_id: str = "c_001",
    currency: str = "RUB",
    status: TransferState = TransferState.COMPLETED,
) -> str:
    transfer_id = f"tr_hist_{uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as session:
        session.add(
            Transfers(
                transfer_id=transfer_id,
                quote_id=f"q_{transfer_id}",
                client_id=client_id,
                idempotency_key=f"hist-{transfer_id}",
                status=status,
                direction_id="RU-TH-BANK",
                source_amount=amount,
                source_currency=currency,
                fee_amount=Decimal("0"),
                fee_currency=currency,
                target_amount=Decimal("0"),
                target_currency="THB",
                recipient={},
                attempts=[],
                transitions=[],
                created_at=created_at,
            )
        )
        await session.commit()
    return transfer_id


@pytest.mark.anyio
async def test_worker_accepts_pending_transfer_and_keeps_hold():
    async with transfer_app() as (app, client):
        quote_id = await create_quote(client)
        status_code, created = await create_transfer(client, quote_id)
        assert status_code == 202
        assert created["status"] == "PENDING"

        available, reserved = await balance_of()
        assert available == START_BALANCE - TRANSFER_AMOUNT
        assert reserved == TRANSFER_AMOUNT

        fake = FakeProviderClient()
        worker = TransferWorker(app.state, provider_client=fake)
        assert await worker.process_pending() == 1

        transfer = await transfer_row(created["transfer_id"])
        assert transfer.status == TransferState.PROCESSING
        assert transfer.provider_id == "prov_alpha"
        assert transfer.provider_payout_id == "po_prov_alpha"
        assert len(fake.calls) == 1
        assert fake.calls[0].idempotency_key == (
            f"{created['transfer_id']}-prov_alpha"
        )
        assert fake.calls[0].amount == "2068.50"
        assert fake.calls[0].currency == "THB"

        # Funds stay reserved while the payout is in flight.
        assert await balance_of() == (available, reserved)
        await assert_funds_invariant()

        # A second poll cannot dispatch the same transfer again.
        assert await worker.process_pending() == 0
        assert len(fake.calls) == 1


@pytest.mark.anyio
async def test_declined_provider_falls_back_to_next_provider():
    async with transfer_app() as (app, client):
        quote_id = await create_quote(client)
        _, created = await create_transfer(client, quote_id)

        fake = FakeProviderClient(
            responses={
                "prov_alpha": declined(),
                "prov_bravo": accepted(),
            }
        )
        worker = TransferWorker(app.state, provider_client=fake)
        await worker.process_pending()

        transfer = await transfer_row(created["transfer_id"])
        assert transfer.status == TransferState.PROCESSING
        assert transfer.provider_id == "prov_bravo"
        assert [call.provider_id for call in fake.calls] == [
            "prov_alpha",
            "prov_bravo",
        ]
        assert [attempt["status"] for attempt in transfer.attempts] == [
            "DECLINED",
            "PROCESSING",
        ]
        available, reserved = await balance_of()
        assert available == START_BALANCE - TRANSFER_AMOUNT
        assert reserved == TRANSFER_AMOUNT
        await assert_funds_invariant()


@pytest.mark.anyio
async def test_all_providers_declining_fails_and_releases_funds():
    async with transfer_app() as (app, client):
        quote_id = await create_quote(client)
        _, created = await create_transfer(client, quote_id)

        fake = FakeProviderClient(
            responses={
                "prov_alpha": declined(),
                "prov_bravo": declined(),
            }
        )
        worker = TransferWorker(app.state, provider_client=fake)
        await worker.process_pending()

        transfer = await transfer_row(created["transfer_id"])
        assert transfer.status == TransferState.FAILED
        assert transfer.failure_code == "PROVIDER_DECLINED"
        assert len(transfer.attempts) == 2
        assert await balance_of() == (START_BALANCE, Decimal("0"))
        await assert_funds_invariant()


@pytest.mark.anyio
async def test_error_5xx_goes_to_review_without_fallback():
    async with transfer_app() as (app, client):
        quote_id = await create_quote(client)
        _, created = await create_transfer(client, quote_id)

        fake = FakeProviderClient(
            responses={
                "prov_alpha": provider_error(500),
                "prov_bravo": accepted(),
            },
            find_results={"prov_alpha": missing()},
        )
        worker = TransferWorker(app.state, provider_client=fake)
        await worker.process_pending()

        transfer = await transfer_row(created["transfer_id"])
        assert transfer.status == TransferState.NEEDS_REVIEW
        assert transfer.failure_code == "HTTP_500"
        # A 5xx may still have created a payout, so the worker must not pay a
        # second provider. The transfer is resolved by reconciliation.
        assert [call.provider_id for call in fake.calls] == ["prov_alpha"]
        assert fake.status_calls[0].kind == "find"
        assert await balance_of() == (
            START_BALANCE - TRANSFER_AMOUNT,
            TRANSFER_AMOUNT,
        )
        await assert_funds_invariant()


@pytest.mark.anyio
async def test_needs_review_is_held_during_the_grace_window():
    async with transfer_app() as (app, client):
        quote_id = await create_quote(client)
        _, created = await create_transfer(client, quote_id)

        fake = FakeProviderClient(
            responses={"prov_alpha": provider_error(500)},
            find_results={"prov_alpha": missing()},
        )
        worker = TransferWorker(app.state, provider_client=fake)
        await worker.process_pending()
        assert (await transfer_row(created["transfer_id"])).status == (
            TransferState.NEEDS_REVIEW
        )

        # Default grace is longer than the test's lifetime: reconciliation
        # must not conclude "no payout" yet.
        assert await worker.reconcile() == 1
        transfer = await transfer_row(created["transfer_id"])
        assert transfer.status == TransferState.NEEDS_REVIEW
        assert await balance_of() == (
            START_BALANCE - TRANSFER_AMOUNT,
            TRANSFER_AMOUNT,
        )


@pytest.mark.anyio
async def test_reconciliation_fails_review_transfer_after_grace():
    async with transfer_app() as (app, client):
        quote_id = await create_quote(client)
        _, created = await create_transfer(client, quote_id)

        fake = FakeProviderClient(
            responses={"prov_alpha": provider_error(500)},
            find_results={"prov_alpha": missing()},
        )
        worker = TransferWorker(app.state, provider_client=fake)
        await worker.process_pending()
        assert (await transfer_row(created["transfer_id"])).status == (
            TransferState.NEEDS_REVIEW
        )

        reconciler = TransferWorker(
            app.state, provider_client=fake, review_grace=0
        )
        assert await reconciler.reconcile() == 1

        transfer = await transfer_row(created["transfer_id"])
        assert transfer.status == TransferState.FAILED
        assert transfer.failure_code == "PROVIDER_ERROR"
        assert await balance_of() == (START_BALANCE, Decimal("0"))
        await assert_funds_invariant()


@pytest.mark.anyio
async def test_timeout_without_payout_goes_to_review_without_fallback():
    async with transfer_app() as (app, client):
        quote_id = await create_quote(client)
        _, created = await create_transfer(client, quote_id)

        fake = FakeProviderClient(
            responses={
                "prov_alpha": timed_out(),
                "prov_bravo": accepted(),
            },
            find_results={"prov_alpha": missing()},
        )
        worker = TransferWorker(app.state, provider_client=fake)
        await worker.process_pending()

        transfer = await transfer_row(created["transfer_id"])
        assert transfer.status == TransferState.NEEDS_REVIEW
        assert transfer.failure_code == "TIMEOUT"
        assert transfer.review_reason
        # An ambiguous timeout must never be retried with another provider.
        assert [call.provider_id for call in fake.calls] == ["prov_alpha"]
        assert await balance_of() == (
            START_BALANCE - TRANSFER_AMOUNT,
            TRANSFER_AMOUNT,
        )
        await assert_funds_invariant()


@pytest.mark.anyio
async def test_failed_status_lookup_goes_to_review_without_fallback():
    async with transfer_app() as (app, client):
        quote_id = await create_quote(client)
        _, created = await create_transfer(client, quote_id)

        fake = FakeProviderClient(
            responses={
                "prov_alpha": timed_out(),
                "prov_bravo": accepted(),
            },
            find_results={"prov_alpha": lookup_failed()},
        )
        worker = TransferWorker(app.state, provider_client=fake)
        await worker.process_pending()

        transfer = await transfer_row(created["transfer_id"])
        assert transfer.status == TransferState.NEEDS_REVIEW
        assert [call.provider_id for call in fake.calls] == ["prov_alpha"]
        assert await balance_of() == (
            START_BALANCE - TRANSFER_AMOUNT,
            TRANSFER_AMOUNT,
        )


@pytest.mark.anyio
async def test_synchronous_provider_completion_debits_funds_and_records_attempt():
    async with transfer_app() as (app, client):
        quote_id = await create_quote(client)
        _, created = await create_transfer(client, quote_id)

        fake = FakeProviderClient(
            responses={"prov_alpha": accepted(status="COMPLETED")}
        )
        worker = TransferWorker(app.state, provider_client=fake)
        await worker.process_pending()

        transfer = await transfer_row(created["transfer_id"])
        assert transfer.status == TransferState.COMPLETED
        assert len(transfer.attempts) == 1
        assert await balance_of() == (
            START_BALANCE - TRANSFER_AMOUNT,
            Decimal("0"),
        )
        assert await completed_total() == TRANSFER_AMOUNT
        await assert_funds_invariant()


class CompletingProviderClient(FakeProviderClient):
    """Simulates a webhook completing while create_payout is still in flight."""

    async def create_payout(self, **kwargs):
        response = await super().create_payout(**kwargs)
        async with AsyncSessionLocal() as session:
            await apply_provider_status(
                session,
                kwargs["reference"],
                "COMPLETED",
                provider_id=kwargs["provider_id"],
                payout_id=response.payout_id,
                source="provider",
                reason="simulated webhook during slow provider response",
            )
        return response


@pytest.mark.anyio
async def test_late_dispatch_cannot_overwrite_a_completed_webhook():
    async with transfer_app() as (app, client):
        quote_id = await create_quote(client)
        _, created = await create_transfer(client, quote_id)

        fake = CompletingProviderClient()
        worker = TransferWorker(app.state, provider_client=fake)
        await worker.process_pending()

        # The webhook won the race; the late create_payout response must not
        # move the transfer back to PROCESSING.
        transfer = await transfer_row(created["transfer_id"])
        assert transfer.status == TransferState.COMPLETED
        assert await balance_of() == (
            START_BALANCE - TRANSFER_AMOUNT,
            Decimal("0"),
        )
        assert await completed_total() == TRANSFER_AMOUNT

        # A later duplicate completion must not debit the already-zero hold.
        async with AsyncSessionLocal() as session:
            await apply_provider_status(
                session,
                created["transfer_id"],
                "COMPLETED",
                provider_id="prov_alpha",
                payout_id="po_prov_alpha",
                source="provider",
                reason="late duplicate webhook",
            )
        assert await balance_of() == (
            START_BALANCE - TRANSFER_AMOUNT,
            Decimal("0"),
        )
        await assert_funds_invariant()


@pytest.mark.anyio
async def test_no_eligible_provider_fails_and_releases_funds():
    async with transfer_app() as (app, client):
        quote_id = await create_quote(client)
        _, created = await create_transfer(client, quote_id)

        fake = FakeProviderClient()
        worker = TransferWorker(app.state, provider_client=fake)
        worker.providers = []
        await worker.process_pending()

        transfer = await transfer_row(created["transfer_id"])
        assert transfer.status == TransferState.FAILED
        assert transfer.failure_code == "NO_ELIGIBLE_PROVIDER"
        assert transfer.attempts == []
        assert fake.calls == []
        assert await balance_of() == (START_BALANCE, Decimal("0"))
        await assert_funds_invariant()


@pytest.mark.anyio
async def test_reconciliation_completes_processing_transfer_with_lost_webhook():
    async with transfer_app() as (app, client):
        quote_id = await create_quote(client)
        _, created = await create_transfer(client, quote_id)

        fake = FakeProviderClient()
        worker = TransferWorker(app.state, provider_client=fake)
        await worker.process_pending()
        assert (await transfer_row(created["transfer_id"])).status == (
            TransferState.PROCESSING
        )

        # The provider settled but the webhook never arrived.
        fake.get_results["prov_alpha"] = found("COMPLETED")
        assert await worker.reconcile() == 1

        transfer = await transfer_row(created["transfer_id"])
        assert transfer.status == TransferState.COMPLETED
        assert await balance_of() == (
            START_BALANCE - TRANSFER_AMOUNT,
            Decimal("0"),
        )
        assert await completed_total() == TRANSFER_AMOUNT
        await assert_funds_invariant()


@pytest.mark.anyio
async def test_reconciliation_resolves_needs_review_transfer():
    async with transfer_app() as (app, client):
        quote_id = await create_quote(client)
        _, created = await create_transfer(client, quote_id)

        fake = FakeProviderClient(
            responses={"prov_alpha": timed_out()},
            find_results={"prov_alpha": missing()},
        )
        worker = TransferWorker(app.state, provider_client=fake)
        await worker.process_pending()
        assert (await transfer_row(created["transfer_id"])).status == (
            TransferState.NEEDS_REVIEW
        )

        fake.find_results["prov_alpha"] = found("COMPLETED")
        reconciler = TransferWorker(
            app.state, provider_client=fake, review_grace=0
        )
        assert await reconciler.reconcile() == 1

        transfer = await transfer_row(created["transfer_id"])
        assert transfer.status == TransferState.COMPLETED
        assert await balance_of() == (
            START_BALANCE - TRANSFER_AMOUNT,
            Decimal("0"),
        )
        await assert_funds_invariant()


@pytest.mark.anyio
async def test_reconciliation_does_not_touch_terminal_transfers():
    async with transfer_app() as (app, client):
        quote_id = await create_quote(client)
        _, created = await create_transfer(client, quote_id)

        fake = FakeProviderClient(
            responses={"prov_alpha": accepted(status="COMPLETED")}
        )
        worker = TransferWorker(app.state, provider_client=fake)
        await worker.process_pending()

        fake.get_results["prov_alpha"] = found("FAILED")
        assert await worker.reconcile() == 0
        assert (await transfer_row(created["transfer_id"])).status == (
            TransferState.COMPLETED
        )
        assert await balance_of() == (
            START_BALANCE - TRANSFER_AMOUNT,
            Decimal("0"),
        )


@pytest.mark.anyio
async def test_claim_lease_prevents_duplicate_workers():
    async with transfer_app() as (_app, client):
        quote_id = await create_quote(client)
        await create_transfer(client, quote_id)

        async with AsyncSessionLocal() as session:
            first = await TransferRepository.claim_pending(
                session, 10, timedelta(seconds=60)
            )
        assert len(first) == 1

        async with AsyncSessionLocal() as session:
            second = await TransferRepository.claim_pending(
                session, 10, timedelta(seconds=60)
            )
        assert second == []

        # Once the lease expires a crashed worker's transfer can be retried.
        await asyncio.sleep(0.01)
        async with AsyncSessionLocal() as session:
            third = await TransferRepository.claim_pending(
                session, 10, timedelta(seconds=0)
            )
        assert len(third) == 1


@pytest.mark.anyio
async def test_idempotency_key_reused_with_different_body_returns_409():
    async with transfer_app() as (_app, client):
        quote_id = await create_quote(client)
        key = "same-key"
        status_code, first = await create_transfer(client, quote_id, key=key)
        assert status_code == 202

        status_code, error = await create_transfer(
            client,
            quote_id,
            key=key,
            recipient={**RECIPIENT, "account": "9999999999"},
        )
        assert status_code == 409
        assert error["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
        assert await count_transfers() == 1
        assert await balance_of() == (
            START_BALANCE - TRANSFER_AMOUNT,
            TRANSFER_AMOUNT,
        )


@pytest.mark.anyio
async def test_concurrent_requests_with_same_key_create_one_transfer():
    async with transfer_app() as (_app, client):
        quote_id = await create_quote(client)
        key = "concurrent-key"

        async def submit():
            return await create_transfer(client, quote_id, key=key)

        results = await asyncio.gather(submit(), submit())
        assert [status for status, _ in results] == [202, 202]
        assert results[0][1]["transfer_id"] == results[1][1]["transfer_id"]
        assert await count_transfers() == 1
        assert await balance_of() == (
            START_BALANCE - TRANSFER_AMOUNT,
            TRANSFER_AMOUNT,
        )
        await assert_funds_invariant()


@pytest.mark.anyio
async def test_concurrent_requests_with_same_key_and_different_body_conflict():
    async with transfer_app() as (_app, client):
        quote_id = await create_quote(client)
        key = "concurrent-conflict"

        async def submit(account: str):
            return await create_transfer(
                client,
                quote_id,
                key=key,
                recipient={**RECIPIENT, "account": account},
            )

        results = await asyncio.gather(submit("1111111111"), submit("2222222222"))
        statuses = sorted(status for status, _ in results)
        assert statuses == [202, 409]
        assert await count_transfers() == 1
        assert await balance_of() == (
            START_BALANCE - TRANSFER_AMOUNT,
            TRANSFER_AMOUNT,
        )


@pytest.mark.anyio
async def test_idempotent_replay_returns_current_status_after_dispatch():
    async with transfer_app() as (app, client):
        quote_id = await create_quote(client)
        key = "replay-after-dispatch"
        _, created = await create_transfer(client, quote_id, key=key)

        worker = TransferWorker(app.state, provider_client=FakeProviderClient())
        await worker.process_pending()

        status_code, replayed = await create_transfer(client, quote_id, key=key)
        assert status_code == 202
        assert replayed["transfer_id"] == created["transfer_id"]
        assert replayed["status"] == "PROCESSING"
        assert await count_transfers() == 1


@pytest.mark.anyio
async def test_daily_limit_exceeded_rolls_back_hold_and_transfer():
    async with transfer_app() as (_app, client):
        first_quote = await create_quote(client, "250000.00")
        status_code, _ = await create_transfer(client, first_quote)
        assert status_code == 202

        second_quote = await create_quote(client, "60000.00")
        status_code, error = await create_transfer(client, second_quote)
        assert status_code == 422
        assert error["error"]["code"] == "DAILY_LIMIT_EXCEEDED"

        assert await count_transfers() == 1
        assert await balance_of() == (
            Decimal("250000.00"),
            Decimal("250000.00"),
        )
        await assert_funds_invariant()


@pytest.mark.anyio
async def test_daily_limit_ignores_transfers_older_than_24_hours():
    async with transfer_app() as (_app, client):
        await insert_historical_transfer(
            Decimal("300000.00"),
            datetime.now(timezone.utc) - timedelta(hours=25),
        )
        quote_id = await create_quote(client)
        status_code, _ = await create_transfer(client, quote_id)
        assert status_code == 202


@pytest.mark.anyio
async def test_daily_limit_counts_transfers_inside_24_hours():
    async with transfer_app() as (_app, client):
        await insert_historical_transfer(
            Decimal("300000.00"),
            datetime.now(timezone.utc) - timedelta(hours=23),
        )
        quote_id = await create_quote(client)
        status_code, error = await create_transfer(client, quote_id)
        assert status_code == 422
        assert error["error"]["code"] == "DAILY_LIMIT_EXCEEDED"


@pytest.mark.anyio
async def test_daily_limit_converts_source_amount_into_limit_currency():
    async with transfer_app() as (_app, client):
        # c_003 has a 5000 USD limit and a RUB balance. 300000 RUB is 3600
        # USD at the snapshot rate, so a second transfer exceeds the limit.
        first_quote = await create_quote(client, "300000.00")
        status_code, _ = await create_transfer(
            client, first_quote, client_id="c_003"
        )
        assert status_code == 202

        second_quote = await create_quote(client, "300000.00")
        status_code, error = await create_transfer(
            client, second_quote, client_id="c_003"
        )
        assert status_code == 422
        assert error["error"]["code"] == "DAILY_LIMIT_EXCEEDED"
        assert await balance_of("c_003") == (
            Decimal("1700000.00"),
            Decimal("300000.00"),
        )
