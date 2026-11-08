import hashlib
import hmac
import json
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from transfercore.infrastructure.db import get_db
from transfercore.infrastructure.repositories import TransferRepository
from transfercore.infrastructure.tables import WebhookEvent
from transfercore.infrastructure.transfer_worker import apply_provider_status

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

VALID_STATUSES = {"PROCESSING", "COMPLETED", "FAILED"}


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


@router.post("/{provider_id}", status_code=200)
async def receive_webhook(
    provider_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    raw = await request.body()
    secret = os.environ.get(
        "PROVIDER_WEBHOOK_SECRET", "local-dev-webhook-secret"
    )
    signature = request.headers.get("X-Signature")
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        return _error(
            401,
            "INVALID_SIGNATURE",
            "Webhook signature is missing or invalid",
        )

    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return _error(400, "INVALID_EVENT", "Webhook body is not valid JSON")

    if not isinstance(data, dict):
        return _error(400, "INVALID_EVENT", "Webhook body must be a JSON object")

    event_id = data.get("event_id")
    reference = data.get("reference")
    payout_id = data.get("payout_id")
    status_value = data.get("status")
    if (
        not isinstance(event_id, str)
        or not event_id
        or not isinstance(reference, str)
        or not reference
        or not isinstance(status_value, str)
        or status_value.upper() not in VALID_STATUSES
    ):
        return _error(
            400,
            "INVALID_EVENT",
            "event_id, reference and a valid status are required",
        )

    status_value = status_value.upper()
    try:
        occurred_at = _parse_time(data.get("occurred_at"))
    except ValueError:
        return _error(400, "INVALID_EVENT", "occurred_at is not a valid timestamp")

    event_stmt = (
        pg_insert(WebhookEvent)
        .values(
            event_id=event_id,
            provider_id=provider_id,
            transfer_id=reference,
            status=status_value,
            occurred_at=occurred_at,
        )
        .on_conflict_do_nothing(index_elements=["event_id"])
        .returning(WebhookEvent.event_id)
    )
    inserted = (await session.execute(event_stmt)).scalar_one_or_none()
    if inserted is None:
        await session.commit()
        return {"status": "duplicate"}

    transfer = await TransferRepository.get_by_id(session, reference)
    if transfer is None:
        await session.commit()
        return {"status": "ignored", "reason": "unknown transfer"}

    if transfer.provider_id and transfer.provider_id != provider_id:
        await session.commit()
        return {"status": "ignored", "reason": "provider mismatch"}

    if (
        transfer.provider_payout_id
        and payout_id
        and transfer.provider_payout_id != payout_id
    ):
        await session.commit()
        return {"status": "ignored", "reason": "payout mismatch"}

    _, applied = await apply_provider_status(
        session,
        transfer.transfer_id,
        status_value,
        provider_id=provider_id,
        payout_id=payout_id if isinstance(payout_id, str) else None,
        failure_code=data.get("failure_code"),
        occurred_at=occurred_at,
        source="provider",
        reason=f"webhook {event_id}",
    )
    if not applied:
        await session.commit()
    return {"status": "accepted" if applied else "ignored"}
