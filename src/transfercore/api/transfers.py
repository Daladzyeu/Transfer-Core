from datetime import datetime, timezone
from decimal import Decimal
import uuid
from fastapi import APIRouter, Depends, Header, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from transfercore.domain.money import Money, registry
from transfercore.domain.quote_engine import (
    DailyLimitExceededError,
    QuoteExpiredError,
    QuoteNotFoundError,
    ValidationError,
)
from transfercore.domain.provider_router import ProviderRouter
from transfercore.infrastructure.db import get_db
from transfercore.infrastructure.provider_client import MockProviderClient
from transfercore.infrastructure.repositories import (
    BalanceRepository,
    QuoteRepository,
    TransferRepository,
)
from transfercore.infrastructure.tables import Transfers, TransferState

router = APIRouter(prefix="/transfers", tags=["transfers"])


class RecipientSchema(BaseModel):
    name: str = Field(..., example="Somchai Jaidee")
    account: str = Field(..., example="1234567890")
    bank_code: str = Field(..., example="BKKB")


class TransferCreateRequest(BaseModel):
    quote_id: str
    client_id: str
    recipient: RecipientSchema


def transfer_to_response(transfer: Transfers, reg=registry) -> dict:
    source_curr = transfer.source_currency or "RUB"
    target_curr = transfer.target_currency or "THB"
    source_amt_str = (
        Money.from_decimal(Decimal(str(transfer.source_amount)), source_curr, reg).to_str()
        if transfer.source_amount is not None
        else "0.00"
    )
    target_amt_str = (
        Money.from_decimal(Decimal(str(transfer.target_amount)), target_curr, reg).to_str()
        if transfer.target_amount is not None
        else "0.00"
    )

    created_iso = (
        transfer.created_at.isoformat()
        if hasattr(transfer, "created_at") and transfer.created_at
        else datetime.now(timezone.utc).isoformat()
    )

    return {
        "transfer_id": transfer.transfer_id,
        "quote_id": transfer.quote_id,
        "client_id": transfer.client_id,
        "status": transfer.status.value,
        "source_amount": {
            "amount": source_amt_str,
            "currency": source_curr,
        },
        "target_amount": {
            "amount": target_amt_str,
            "currency": target_curr,
        },
        "recipient": transfer.recipient,
        "attempts": transfer.attempts or [],
        "transitions": transfer.transitions or [],
        "created_at": created_iso,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_transfer(
    body: TransferCreateRequest,
    request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    session: AsyncSession = Depends(get_db),
):
    if not idempotency_key:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Idempotency-Key header is required",
                }
            },
        )

    # 1. Idempotency Check
    existing = await TransferRepository.get_by_idempotency_key(session, idempotency_key)
    if existing:
        return transfer_to_response(existing)

    # 2. Retrieve Quote
    quote = await QuoteRepository.get_by_id(session, body.quote_id)

    # 3. Check Daily Limit
    clients_map = getattr(request.app.state, "clients", {})
    client_info = clients_map.get(body.client_id)
    if client_info and "daily_limit" in client_info:
        limit_cfg = client_info["daily_limit"]
        limit_amount = Decimal(str(limit_cfg.get("amount", "999999999")))
        spent_today = await TransferRepository.calculate_24h_spent(session, body.client_id)
        if spent_today + quote.source_amount.amount > limit_amount:
            raise DailyLimitExceededError("Daily limit exceeded for client")

    # 4. Hold Funds in atomic transaction
    source_curr = quote.source_amount.currency
    source_amt = quote.source_amount.amount
    await BalanceRepository.hold_funds(session, body.client_id, source_curr, source_amt)

    # 5. Create Transfer Entity
    transfer_id = f"tr_{uuid.uuid4().hex[:12]}"
    recipient_dict = body.recipient.model_dump()
    transfer = Transfers(
        transfer_id=transfer_id,
        quote_id=body.quote_id,
        client_id=body.client_id,
        idempotency_key=idempotency_key,
        status=TransferState.PROCESSING,
        source_amount=source_amt,
        source_currency=source_curr,
        target_amount=quote.target_amount.amount,
        target_currency=quote.target_amount.currency,
        recipient=recipient_dict,
        attempts=[],
        transitions=[
            {
                "from": "CREATED",
                "to": "PROCESSING",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
    )
    transfer = await TransferRepository.create_transfer(session, transfer)

    # 6. Provider Selection & Dispatch
    providers_list = getattr(request.app.state, "providers", [])
    rates_map = getattr(request.app.state, "rates_map", {})
    router_engine = ProviderRouter(providers_list, rates_map)

    # Find tariff min_margin_percent
    tariff_margin = Decimal("0.50")
    engine = getattr(request.app.state, "quote_engine", None)
    if engine and hasattr(engine, "_tariffs_by_route"):
        route_tariffs = list(engine._tariffs_by_route.values())
        matching = [t for t in route_tariffs if t.direction_id == quote.direction_id]
        if matching:
            tariff_margin = matching[0].min_margin_percent

    ranked_providers = router_engine.select_providers(
        direction_id=quote.direction_id,
        target_amount=quote.target_amount.amount,
        target_currency=quote.target_amount.currency,
        source_amount=source_amt,
        source_currency=source_curr,
        client_fee=quote.fee.amount,
        min_margin_percent=tariff_margin,
    )

    provider_client = MockProviderClient()
    final_status = TransferState.PROCESSING
    last_attempt = None

    for prov_id in ranked_providers:
        attempt_key = f"{transfer_id}-{prov_id}"
        target_amt_str = quote.target_amount.to_str()

        payout_resp = await provider_client.create_payout(
            provider_id=prov_id,
            idempotency_key=attempt_key,
            reference=transfer_id,
            amount=target_amt_str,
            currency=quote.target_amount.currency,
            recipient=recipient_dict,
        )

        last_attempt = {
            "provider_id": prov_id,
            "payout_id": payout_resp.payout_id,
            "status": payout_resp.status,
            "error_code": payout_resp.error_code,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        if payout_resp.is_success:
            # Payout accepted (201)
            if payout_resp.status == "COMPLETED":
                final_status = TransferState.COMPLETED
                await BalanceRepository.debit_held_funds(session, body.client_id, source_curr, source_amt)
            else:
                final_status = TransferState.PROCESSING
            break
        elif payout_resp.status == "TIMEOUT":
            # Timeout (slow scenario) - must NOT try next provider to avoid double payout!
            final_status = TransferState.PROCESSING
            break
        else:
            # Declined (422) or Error (5xx) - record failure, try next eligible fallback provider
            pass

    # If all eligible providers failed and none accepted
    if last_attempt and last_attempt.get("status") in ("DECLINED", "ERROR") and final_status == TransferState.PROCESSING and len(ranked_providers) > 0:
        # Check if the last attempted provider was a failure
        if last_attempt["status"] != "PROCESSING":
            final_status = TransferState.FAILED
            await BalanceRepository.release_hold(session, body.client_id, source_curr, source_amt)

    # Save final status and attempt
    transfer = await TransferRepository.record_attempt_and_status(
        session, transfer_id, final_status, last_attempt
    )

    reg = getattr(request.app.state, "currency_registry", registry)
    return transfer_to_response(transfer, reg)


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
