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
    status = Column(Enum(TransferState, name="transfer_state"), nullable=False)
    source_amount = Column(Numeric(precision=20, scale=10))
    source_currency = Column(String)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
