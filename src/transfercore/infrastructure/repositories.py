from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from transfercore.domain.money import CurrencyRegistry, Money, registry
from transfercore.domain.quote_engine import (
    InsufficientFundsError,
    QuoteExpiredError,
    QuoteNotFoundError,
    QuoteResult,
    RateUnavailableError,
)
from transfercore.domain.rates import RateNotAvailable, RateService
from transfercore.infrastructure.tables import (
    ClientBalance,
    Quotes,
    Transfers,
    TransferState,
)

COUNTED_DAILY_LIMIT_STATES = (
    TransferState.PENDING,
    TransferState.PROCESSING,
    TransferState.COMPLETED,
    TransferState.NEEDS_REVIEW,
)


class QuoteRepository:
    @staticmethod
    async def get_by_id(session: AsyncSession, quote_id: str) -> QuoteResult:
        stmt = select(Quotes).where(Quotes.quote_id == quote_id)
        result = await session.execute(stmt)

        db_quote = result.scalar_one_or_none()

        if not db_quote:
            raise QuoteNotFoundError(f"Quote {quote_id} not found")

        expires_at = db_quote.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        current_time = datetime.now(timezone.utc)

        if current_time >= expires_at:
            raise QuoteExpiredError(f"Quote {quote_id} has expired")

        payload = db_quote.payload

        expires_str = expires_at.isoformat().replace("+00:00", "Z")

        def parse_money(data: dict) -> Money:
            return Money(amount=Decimal(str(data["amount"])), currency=data["currency"])

        return QuoteResult(
            quote_id=db_quote.quote_id,
            direction_id=payload["direction_id"],
            source_amount=parse_money(payload["source_amount"]),
            fee=parse_money(payload["fee"]),
            amount_to_convert=parse_money(payload["amount_to_convert"]),
            rate=Decimal(str(payload["rate"])),
            target_amount=parse_money(payload["target_amount"]),
            breakdown=payload.get("breakdown"),
            expires_at=expires_str,
            rate_updated_at=payload.get("rate_updated_at"),
        )

    @staticmethod
    async def save(
        session: AsyncSession,
        quote: QuoteResult,
        registry: CurrencyRegistry = registry,
    ) -> QuoteResult:
        expires_str = quote.expires_at.replace("Z", "+00:00")
        expires_at_dt = datetime.fromisoformat(expires_str)
        if expires_at_dt.tzinfo is None:
            expires_at_dt = expires_at_dt.replace(tzinfo=timezone.utc)

        def money_payload(money: Money) -> dict:
            return {
                "amount": money.to_str(registry),
                "currency": money.currency,
            }

        payload_data = {
            "direction_id": quote.direction_id,
            "source_amount": money_payload(quote.source_amount),
            "fee": money_payload(quote.fee),
            "amount_to_convert": money_payload(quote.amount_to_convert),
            "rate": str(quote.rate),
            "target_amount": money_payload(quote.target_amount),
            "breakdown": quote.breakdown,
            "rate_updated_at": quote.rate_updated_at,
        }

        db_quote = Quotes(
            quote_id=quote.quote_id, payload=payload_data, expires_at=expires_at_dt
        )
        session.add(db_quote)
        await session.commit()
        await session.refresh(db_quote)
        return quote


class BalanceRepository:
    @staticmethod
    async def seed_balances(session: AsyncSession, clients_data: list[dict]) -> None:
        """Seed initial balances from clients.json if the balances table is empty."""
        stmt = select(func.count()).select_from(ClientBalance)
        count = await session.scalar(stmt)
        if count and count > 0:
            await session.rollback()
            return

        for client in clients_data:
            client_id = client["client_id"]
            for currency, amount in client.get("balances", {}).items():
                balance = ClientBalance(
                    client_id=client_id,
                    currency=currency,
                    available=Decimal(str(amount)),
                    reserved=Decimal("0"),
                )
                session.add(balance)
        await session.commit()

    @staticmethod
    async def get_balances_by_client(
        session: AsyncSession, client_id: str
    ) -> list[ClientBalance]:
        """Fetch all currency balances for a given client."""
        stmt = (
            select(ClientBalance)
            .where(ClientBalance.client_id == client_id)
            .order_by(ClientBalance.currency)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def get_balance_for_update(
        session: AsyncSession, client_id: str, currency: str
    ) -> ClientBalance | None:
        """Pessimistically lock and retrieve client balance row."""
        stmt = (
            select(ClientBalance)
            .where(
                ClientBalance.client_id == client_id,
                ClientBalance.currency == currency,
            )
            .with_for_update()
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def hold_funds(
        session: AsyncSession, client_id: str, currency: str, amount: Decimal
    ) -> ClientBalance:
        """
        Hold funds atomically: available -= amount, reserved += amount.
        Must be called within an active transaction.
        """
        balance = await BalanceRepository.get_balance_for_update(
            session, client_id, currency
        )
        if not balance or balance.available < amount:
            available_val = balance.available if balance else Decimal("0")
            raise InsufficientFundsError(
                f"Insufficient funds: available {available_val} < {amount}"
            )

        balance.available -= amount
        balance.reserved += amount
        session.add(balance)
        return balance

    @staticmethod
    async def release_hold(
        session: AsyncSession, client_id: str, currency: str, amount: Decimal
    ) -> ClientBalance:
        """
        Release held funds on transfer failure: reserved -= amount, available += amount.
        """
        balance = await BalanceRepository.get_balance_for_update(
            session, client_id, currency
        )
        if not balance:
            raise InsufficientFundsError(f"Balance for client {client_id} not found")

        balance.reserved -= amount
        balance.available += amount
        session.add(balance)
        return balance

    @staticmethod
    async def debit_held_funds(
        session: AsyncSession, client_id: str, currency: str, amount: Decimal
    ) -> ClientBalance:
        """
        Finalize transfer on successful payout: reserved -= amount.
        """
        balance = await BalanceRepository.get_balance_for_update(
            session, client_id, currency
        )
        if not balance:
            raise InsufficientFundsError(f"Balance for client {client_id} not found")

        balance.reserved -= amount
        session.add(balance)
        return balance


class TransferRepository:
    @staticmethod
    async def get_by_id(session: AsyncSession, transfer_id: str) -> Transfers | None:
        stmt = select(Transfers).where(Transfers.transfer_id == transfer_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_id_for_update(
        session: AsyncSession, transfer_id: str
    ) -> Transfers | None:
        stmt = (
            select(Transfers)
            .where(Transfers.transfer_id == transfer_id)
            .with_for_update()
            # A caller may already have loaded this transfer earlier in the
            # same session (dispatch, webhook validation). Settlement must
            # always act on the committed row, otherwise a slow provider
            # response can overwrite a webhook that arrived in the meantime.
            .execution_options(populate_existing=True)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_idempotency_key(
        session: AsyncSession, idempotency_key: str
    ) -> Transfers | None:
        stmt = select(Transfers).where(Transfers.idempotency_key == idempotency_key)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def create_pending_transfer(
        session: AsyncSession,
        transfer: Transfers,
    ) -> tuple[Transfers, bool]:
        """Insert a transfer, or return the existing one for a repeated key.

        The unique index on ``idempotency_key`` makes concurrent submissions
        safe: the second insert blocks until the first transaction commits and
        then returns no row.
        """
        stmt = (
            pg_insert(Transfers)
            .values(
                transfer_id=transfer.transfer_id,
                quote_id=transfer.quote_id,
                client_id=transfer.client_id,
                idempotency_key=transfer.idempotency_key,
                request_fingerprint=transfer.request_fingerprint,
                status=transfer.status,
                direction_id=transfer.direction_id,
                source_amount=transfer.source_amount,
                source_currency=transfer.source_currency,
                fee_amount=transfer.fee_amount,
                fee_currency=transfer.fee_currency,
                target_amount=transfer.target_amount,
                target_currency=transfer.target_currency,
                recipient=transfer.recipient,
                attempts=transfer.attempts or [],
                transitions=transfer.transitions or [],
            )
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
            .returning(Transfers.transfer_id)
        )
        inserted_id = (await session.execute(stmt)).scalar_one_or_none()
        if inserted_id is None:
            existing = await TransferRepository.get_by_idempotency_key(
                session, transfer.idempotency_key
            )
            return existing, False

        created = await TransferRepository.get_by_id(session, inserted_id)
        return created, True

    @staticmethod
    async def calculate_24h_spent(
        session: AsyncSession,
        client_id: str,
        limit_currency: str,
        rate_service: RateService,
        exclude_transfer_id: str | None = None,
    ) -> Decimal:
        """Rolling 24-hour volume for a client, expressed in limit currency."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        stmt = select(Transfers.source_amount, Transfers.source_currency).where(
            Transfers.client_id == client_id,
            Transfers.status.in_(COUNTED_DAILY_LIMIT_STATES),
            Transfers.created_at >= cutoff,
        )
        if exclude_transfer_id is not None:
            stmt = stmt.where(Transfers.transfer_id != exclude_transfer_id)

        rows = (await session.execute(stmt)).all()
        total = Decimal("0")
        for amount, currency in rows:
            if amount is None or currency is None:
                continue
            value = Decimal(str(amount))
            if currency == limit_currency:
                total += value
                continue
            try:
                total += rate_service.convert(value, currency, limit_currency)
            except RateNotAvailable as exc:
                raise RateUnavailableError(
                    currency, limit_currency, exc.details
                ) from exc
        return total

    @staticmethod
    async def claim_pending(
        session: AsyncSession,
        limit: int,
        claim_timeout: timedelta,
    ) -> list[Transfers]:
        """Claim queued transfers with a lease so a crashed worker can retry."""
        now = datetime.now(timezone.utc)
        stmt = (
            select(Transfers)
            .where(
                Transfers.status == TransferState.PENDING,
                or_(
                    Transfers.claimed_at.is_(None),
                    Transfers.claimed_at < now - claim_timeout,
                ),
            )
            .order_by(Transfers.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = list((await session.execute(stmt)).scalars().all())
        for row in rows:
            row.claimed_at = now
        await session.commit()
        return rows

    @staticmethod
    async def get_reconcilable(
        session: AsyncSession,
        limit: int,
        min_age: timedelta,
    ) -> list[Transfers]:
        now = datetime.now(timezone.utc)
        stmt = (
            select(Transfers)
            .where(
                Transfers.status.in_(
                    (TransferState.PROCESSING, TransferState.NEEDS_REVIEW)
                ),
                Transfers.provider_id.is_not(None),
                or_(
                    Transfers.last_reconciled_at.is_(None),
                    Transfers.last_reconciled_at < now - min_age,
                ),
            )
            .order_by(Transfers.updated_at.asc().nullsfirst())
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())

    @staticmethod
    async def record_attempt_and_status(
        session: AsyncSession,
        transfer_id: str,
        new_status: TransferState,
        attempt: dict | None = None,
    ) -> Transfers | None:
        stmt = (
            select(Transfers)
            .where(Transfers.transfer_id == transfer_id)
            .with_for_update()
        )
        result = await session.execute(stmt)
        transfer = result.scalar_one_or_none()
        if not transfer:
            return None

        # Record state transition
        transitions = list(transfer.transitions or [])
        transitions.append(
            {
                "from": transfer.status.value,
                "to": new_status.value,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )
        transfer.status = new_status
        transfer.transitions = transitions
        transfer.claimed_at = None

        # Record provider attempt
        if attempt:
            attempts = list(transfer.attempts or [])
            attempts.append(attempt)
            transfer.attempts = attempts

        session.add(transfer)
        await session.commit()
        await session.refresh(transfer)
        return transfer
