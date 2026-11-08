import hashlib
import hmac
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from transfercore.api.app import create_app
from transfercore.infrastructure.db import AsyncSessionLocal
from transfercore.infrastructure.repositories import (
    BalanceRepository,
    TransferRepository,
)
from transfercore.infrastructure.tables import TransferState
from transfercore.infrastructure.transfer_worker import TransferWorker

from fakes import FakeProviderClient, missing, timed_out

SECRET = "local-dev-webhook-secret"
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


def timestamp(hour: int) -> str:
    return f"2026-11-08T{hour:02d}:00:00Z"


def event_payload(
    transfer_id: str,
    status: str,
    *,
    event_id: str | None = None,
    occurred_at: str = "2026-11-08T12:00:00Z",
    provider_id: str = "prov_alpha",
    payout_id: str = "po_prov_alpha",
    **extra,
) -> dict:
    payload = {
        "event_id": event_id or f"ev_{uuid.uuid4().hex[:10]}",
        "payout_id": payout_id,
        "reference": transfer_id,
        "status": status,
        "occurred_at": occurred_at,
    }
    payload.update(extra)
    return payload


async def post_webhook(
    client: AsyncClient,
    payload: dict | bytes,
    *,
    provider_id: str = "prov_alpha",
    secret: str = SECRET,
    include_signature: bool = True,
):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if include_signature:
        headers["X-Signature"] = hmac.new(
            secret.encode(), body, hashlib.sha256
        ).hexdigest()
    return await client.post(
        f"/webhooks/{provider_id}", content=body, headers=headers
    )


async def balance_of():
    async with AsyncSessionLocal() as session:
        balance = await BalanceRepository.get_balance_for_update(
            session, "c_001", "RUB"
        )
        assert balance is not None
        return Decimal(str(balance.available)), Decimal(str(balance.reserved))


async def transfer_row(transfer_id: str):
    async with AsyncSessionLocal() as session:
        transfer = await TransferRepository.get_by_id(session, transfer_id)
        assert transfer is not None
        return transfer


@asynccontextmanager
async def processing_transfer(*, fake: FakeProviderClient | None = None):
    app = create_app(start_worker=False)
    fake = fake or FakeProviderClient()
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            quote = await client.post("/quotes", json=QUOTE_REQUEST)
            assert quote.status_code == 201
            created = await client.post(
                "/transfers",
                json={
                    "quote_id": quote.json()["quote_id"],
                    "client_id": "c_001",
                    "recipient": RECIPIENT,
                },
                headers={"Idempotency-Key": f"wh-{uuid.uuid4().hex}"},
            )
            assert created.status_code == 202
            transfer_id = created.json()["transfer_id"]

            worker = TransferWorker(app.state, provider_client=fake)
            await worker.process_pending()
            yield client, transfer_id, fake


@pytest.mark.anyio
async def test_completed_webhook_debits_hold_and_duplicate_is_ignored():
    async with processing_transfer() as (client, transfer_id, _fake):
        payload = event_payload(transfer_id, "COMPLETED")

        response = await post_webhook(client, payload)
        assert response.status_code == 200
        assert response.json() == {"status": "accepted"}

        transfer = await transfer_row(transfer_id)
        assert transfer.status == TransferState.COMPLETED
        assert await balance_of() == (
            START_BALANCE - TRANSFER_AMOUNT,
            Decimal("0"),
        )

        duplicate = await post_webhook(client, payload)
        assert duplicate.status_code == 200
        assert duplicate.json() == {"status": "duplicate"}
        assert await balance_of() == (
            START_BALANCE - TRANSFER_AMOUNT,
            Decimal("0"),
        )
        transitions = await transfer_row(transfer_id)
        assert [t["to"] for t in transitions.transitions] == [
            "PENDING",
            "PROCESSING",
            "COMPLETED",
        ]


@pytest.mark.anyio
async def test_webhook_with_invalid_or_missing_signature_is_rejected():
    async with processing_transfer() as (client, transfer_id, _fake):
        payload = event_payload(transfer_id, "COMPLETED")

        invalid = await post_webhook(client, payload, secret="wrong-secret")
        assert invalid.status_code == 401
        assert invalid.json()["error"]["code"] == "INVALID_SIGNATURE"

        missing_signature = await post_webhook(
            client, payload, include_signature=False
        )
        assert missing_signature.status_code == 401
        assert missing_signature.json()["error"]["code"] == "INVALID_SIGNATURE"

        assert (await transfer_row(transfer_id)).status == (
            TransferState.PROCESSING
        )
        assert await balance_of() == (
            START_BALANCE - TRANSFER_AMOUNT,
            TRANSFER_AMOUNT,
        )


@pytest.mark.anyio
async def test_unknown_transfer_event_is_acknowledged_and_ignored():
    async with processing_transfer() as (client, _transfer_id, _fake):
        response = await post_webhook(
            client, event_payload("tr_does_not_exist", "COMPLETED")
        )
        assert response.status_code == 200
        assert response.json()["status"] == "ignored"
        assert response.json()["reason"] == "unknown transfer"


@pytest.mark.anyio
async def test_event_from_another_provider_is_ignored():
    async with processing_transfer() as (client, transfer_id, _fake):
        response = await post_webhook(
            client,
            event_payload(transfer_id, "COMPLETED"),
            provider_id="prov_bravo",
        )
        assert response.status_code == 200
        assert response.json() == {
            "status": "ignored",
            "reason": "provider mismatch",
        }
        assert (await transfer_row(transfer_id)).status == (
            TransferState.PROCESSING
        )


@pytest.mark.anyio
async def test_event_with_another_payout_id_is_ignored():
    async with processing_transfer() as (client, transfer_id, _fake):
        response = await post_webhook(
            client,
            event_payload(
                transfer_id, "COMPLETED", payout_id="po_someone_else"
            ),
        )
        assert response.status_code == 200
        assert response.json() == {
            "status": "ignored",
            "reason": "payout mismatch",
        }
        assert (await transfer_row(transfer_id)).status == (
            TransferState.PROCESSING
        )


@pytest.mark.anyio
async def test_out_of_order_event_does_not_move_transfer_backwards():
    async with processing_transfer() as (client, transfer_id, _fake):
        # A newer PROCESSING event establishes the high-water mark.
        newer = await post_webhook(
            client,
            event_payload(transfer_id, "PROCESSING", occurred_at=timestamp(12)),
        )
        assert newer.json() == {"status": "accepted"}

        # An older COMPLETED event arrives late and must be ignored.
        stale = await post_webhook(
            client,
            event_payload(transfer_id, "COMPLETED", occurred_at=timestamp(10)),
        )
        assert stale.json() == {"status": "ignored"}
        assert (await transfer_row(transfer_id)).status == (
            TransferState.PROCESSING
        )

        # A newer COMPLETED event is still applied.
        fresh = await post_webhook(
            client,
            event_payload(transfer_id, "COMPLETED", occurred_at=timestamp(13)),
        )
        assert fresh.json() == {"status": "accepted"}
        assert (await transfer_row(transfer_id)).status == TransferState.COMPLETED
        assert await balance_of() == (
            START_BALANCE - TRANSFER_AMOUNT,
            Decimal("0"),
        )


@pytest.mark.anyio
async def test_terminal_status_is_sticky_and_funds_are_released_once():
    async with processing_transfer() as (client, transfer_id, _fake):
        failed = await post_webhook(
            client,
            event_payload(
                transfer_id,
                "FAILED",
                occurred_at=timestamp(12),
                failure_code="BENEFICIARY_BANK_REJECTED",
            ),
        )
        assert failed.json() == {"status": "accepted"}
        assert (await transfer_row(transfer_id)).status == TransferState.FAILED
        assert await balance_of() == (START_BALANCE, Decimal("0"))

        completed = await post_webhook(
            client,
            event_payload(transfer_id, "COMPLETED", occurred_at=timestamp(13)),
        )
        assert completed.json() == {"status": "ignored"}
        assert (await transfer_row(transfer_id)).status == TransferState.FAILED
        assert await balance_of() == (START_BALANCE, Decimal("0"))


@pytest.mark.anyio
async def test_processing_webhook_clears_needs_review():
    fake = FakeProviderClient(
        responses={"prov_alpha": timed_out()},
        find_results={"prov_alpha": missing()},
    )
    async with processing_transfer(fake=fake) as (client, transfer_id, _fake):
        assert (await transfer_row(transfer_id)).status == (
            TransferState.NEEDS_REVIEW
        )

        response = await post_webhook(
            client,
            event_payload(transfer_id, "PROCESSING", occurred_at=timestamp(12)),
        )
        assert response.json() == {"status": "accepted"}

        transfer = await transfer_row(transfer_id)
        assert transfer.status == TransferState.PROCESSING
        assert transfer.review_reason is None
        assert await balance_of() == (
            START_BALANCE - TRANSFER_AMOUNT,
            TRANSFER_AMOUNT,
        )


@pytest.mark.anyio
async def test_invalid_event_payloads_are_rejected():
    async with processing_transfer() as (client, transfer_id, _fake):
        unknown_status = await post_webhook(
            client, event_payload(transfer_id, "PAID")
        )
        assert unknown_status.status_code == 400
        assert unknown_status.json()["error"]["code"] == "INVALID_EVENT"

        missing_event_id = await post_webhook(
            client,
            {
                "payout_id": "po_prov_alpha",
                "reference": transfer_id,
                "status": "COMPLETED",
            },
        )
        assert missing_event_id.status_code == 400
        assert missing_event_id.json()["error"]["code"] == "INVALID_EVENT"

        malformed = await post_webhook(client, b"{not-json")
        assert malformed.status_code == 400
        assert malformed.json()["error"]["code"] == "INVALID_EVENT"


@pytest.mark.anyio
async def test_webhook_without_occurred_at_is_applied():
    async with processing_transfer() as (client, transfer_id, _fake):
        payload = event_payload(transfer_id, "COMPLETED")
        payload.pop("occurred_at")

        response = await post_webhook(client, payload)
        assert response.json() == {"status": "accepted"}
        assert (await transfer_row(transfer_id)).status == TransferState.COMPLETED
