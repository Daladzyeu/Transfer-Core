import enum
from decimal import Decimal
import os

from sqlalchemy import Column, DateTime, Enum, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB

from transfercore.infrastructure.db import Base


class TransferState(enum.Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class ClientBalance(Base):
    __tablename__ = "balances"

    client_id = Column(String, primary_key=True)
    currency = Column(String, primary_key=True)
    available = Column(Numeric(precision=20, scale=10), nullable=False)
    reserved = Column(Numeric, default=Decimal("0"), nullable=False)


class Quotes(Base):
    __tablename__ = "quotes"

    quote_id = Column(String, primary_key=True)
    payload = Column(JSONB, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Transfers(Base):
    __tablename__ = "transfers"

    transfer_id = Column(String, primary_key=True)
    quote_id = Column(String, nullable=False)
    client_id = Column(String, nullable=False)
    idempotency_key = Column(String, unique=True)
    request_fingerprint = Column(String)
    status = Column(
        Enum(TransferState, name="transfer_state"), nullable=False, index=True
    )
    direction_id = Column(String)
    source_amount = Column(Numeric(precision=20, scale=10))
    source_currency = Column(String)
    fee_amount = Column(Numeric(precision=20, scale=10))
    fee_currency = Column(String)
    target_amount = Column(Numeric(precision=20, scale=10))
    target_currency = Column(String)
    recipient = Column(JSONB)
    provider_id = Column(String)
    provider_payout_id = Column(String)
    failure_code = Column(String)
    review_reason = Column(String)
    attempts = Column(JSONB, default=list)
    transitions = Column(JSONB, default=list)
    last_event_at = Column(DateTime(timezone=True))
    last_reconciled_at = Column(DateTime(timezone=True))
    claimed_at = Column(DateTime(timezone=True))
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    event_id = Column(String, primary_key=True)
    provider_id = Column(String, nullable=False)
    transfer_id = Column(String)
    status = Column(String, nullable=False)
    occurred_at = Column(DateTime(timezone=True))
    received_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
