import hashlib
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from transfercore.domain.money import Money, registry
from transfercore.domain.quote_engine import (
    DailyLimitExceededError,
    QuoteError,
    RateUnavailableError,
    ValidationError,
)
from transfercore.domain.rates import RateNotAvailable
from transfercore.infrastructure.db import get_db
from transfercore.infrastructure.repositories import (
    BalanceRepository,
    QuoteRepository,
    TransferRepository,
)
from transfercore.infrastructure.tables import Transfers, TransferState

router = APIRouter(prefix="/transfers", tags=["transfers"])


class ClientNotFoundError(QuoteError):
    def __init__(self, client_id: str):
        super().__init__(
            "CLIENT_NOT_FOUND",
            f"Client {client_id} not found",
            {"client_id": client_id},
        )


class IdempotencyConflictError(QuoteError):
    def __init__(self, idempotency_key: str):
        super().__init__(
            "IDEMPOTENCY_KEY_REUSED",
            "The Idempotency-Key was already used with a different request",
            {"idempotency_key": idempotency_key},
        )


class RecipientSchema(BaseModel):
    name: str = Field(..., min_length=1, example="Somchai Jaidee")
    account: str = Field(..., min_length=1, example="1234567890")
    bank_code: str = Field(..., min_length=1, example="BKKB")


class TransferCreateRequest(BaseModel):
    quote_id: str = Field(..., min_length=1)
    client_id: str = Field(..., min_length=1)
    recipient: RecipientSchema


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def _money_json(
    amount: Decimal | None, currency: str | None, reg
) -> dict | None:
    if amount is None or currency is None:
        return None
    value = Money.from_decimal(Decimal(str(amount)), currency, reg)
    return {"amount": value.to_str(reg), "currency": currency}


def transfer_to_response(transfer: Transfers, reg=registry) -> dict:
    source_curr = transfer.source_currency or "RUB"
    target_curr = transfer.target_currency or "THB"

    return {
        "transfer_id": transfer.transfer_id,
        "quote_id": transfer.quote_id,
        "client_id": transfer.client_id,
        "status": transfer.status.value,
        "direction_id": transfer.direction_id,
        "source_amount": _money_json(
            transfer.source_amount, source_curr, reg
        ),
        "fee": _money_json(transfer.fee_amount, transfer.fee_currency, reg),
        "target_amount": _money_json(
            transfer.target_amount, target_curr, reg
        ),
        "recipient": transfer.recipient,
        "provider_id": transfer.provider_id,
        "provider_payout_id": transfer.provider_payout_id,
        "failure_code": transfer.failure_code,
        "review_reason": transfer.review_reason,
        "attempts": transfer.attempts or [],
        "transitions": transfer.transitions or [],
        "created_at": _iso(transfer.created_at),
        "updated_at": _iso(transfer.updated_at),
    }


def _fingerprint(body: TransferCreateRequest) -> str:
    canonical = json.dumps(
        {
            "quote_id": body.quote_id,
            "client_id": body.client_id,
            "recipient": body.recipient.model_dump(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_transfer(
    body: TransferCreateRequest,
    request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    session: AsyncSession = Depends(get_db),
):
    idempotency_key = (idempotency_key or "").strip()
    if not idempotency_key:
        raise ValidationError(
            "Idempotency-Key header is required",
            {"header": "Idempotency-Key"},
        )

    reg = getattr(request.app.state, "currency_registry", registry)
    fingerprint = _fingerprint(body)

    existing = await TransferRepository.get_by_idempotency_key(
        session, idempotency_key
    )
    if existing is not None:
        if (
            existing.request_fingerprint is not None
            and existing.request_fingerprint != fingerprint
        ):
            raise IdempotencyConflictError(idempotency_key)
        await session.commit()
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=transfer_to_response(existing, reg),
            headers={"Idempotency-Replayed": "true"},
        )

    clients_map = getattr(request.app.state, "clients", {})
    client_info = clients_map.get(body.client_id)
    if client_info is None:
        raise ClientNotFoundError(body.client_id)

    quote = await QuoteRepository.get_by_id(session, body.quote_id)
    now = datetime.now(timezone.utc)
    transfer = Transfers(
        transfer_id=f"tr_{uuid.uuid4().hex[:12]}",
        quote_id=body.quote_id,
        client_id=body.client_id,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        status=TransferState.PENDING,
        direction_id=quote.direction_id,
        source_amount=quote.source_amount.amount,
        source_currency=quote.source_amount.currency,
        fee_amount=quote.fee.amount,
        fee_currency=quote.fee.currency,
        target_amount=quote.target_amount.amount,
        target_currency=quote.target_amount.currency,
        recipient=body.recipient.model_dump(),
        attempts=[],
        transitions=[
            {
                "from": None,
                "to": TransferState.PENDING.value,
                "timestamp": now.isoformat(),
                "source": "client",
            }
        ],
    )

    created, inserted = await TransferRepository.create_pending_transfer(
        session, transfer
    )
    if not inserted:
        if (
            created is not None
            and created.request_fingerprint is not None
            and created.request_fingerprint != fingerprint
        ):
            await session.rollback()
            raise IdempotencyConflictError(idempotency_key)
        await session.commit()
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=transfer_to_response(created, reg),
            headers={"Idempotency-Replayed": "true"},
        )

    try:
        await BalanceRepository.hold_funds(
            session,
            body.client_id,
            quote.source_amount.currency,
            quote.source_amount.amount,
        )

        if "daily_limit" in client_info:
            limit_cfg = client_info["daily_limit"]
            limit_currency = limit_cfg.get(
                "currency", quote.source_amount.currency
            )
            limit_amount = Decimal(str(limit_cfg.get("amount", "0")))
            rate_service = getattr(request.app.state, "rate_service", None)
            if rate_service is None:
                spent = Decimal("0")
                current_in_limit = quote.source_amount.amount
            else:
                spent = await TransferRepository.calculate_24h_spent(
                    session,
                    body.client_id,
                    limit_currency,
                    rate_service,
                    exclude_transfer_id=created.transfer_id,
                )
                try:
                    current_in_limit = rate_service.convert(
                        quote.source_amount.amount,
                        quote.source_amount.currency,
                        limit_currency,
                    )
                except RateNotAvailable as exc:
                    raise RateUnavailableError(
                        quote.source_amount.currency,
                        limit_currency,
                        exc.details,
                    ) from exc
            if spent + current_in_limit > limit_amount:
                raise DailyLimitExceededError(
                    f"Daily limit exceeded for client {body.client_id}",
                    {
                        "limit": str(limit_amount),
                        "currency": limit_currency,
                        "spent": str(spent),
                        "requested": str(current_in_limit),
                    },
                )

        await session.commit()
    except Exception:
        await session.rollback()
        raise

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=transfer_to_response(created, reg),
    )


@router.get("/{transfer_id}", status_code=status.HTTP_200_OK)
async def get_transfer(
    transfer_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    transfer = await TransferRepository.get_by_id(session, transfer_id)
    if not transfer:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "error": {
                    "code": "TRANSFER_NOT_FOUND",
                    "message": f"Transfer {transfer_id} not found",
                }
            },
        )

    reg = getattr(request.app.state, "currency_registry", registry)
    return transfer_to_response(transfer, reg)
