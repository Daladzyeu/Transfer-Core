import dataclasses
from decimal import Decimal

from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from transfercore.domain.quote_engine import (
    InsufficientFundsError,
    QuoteResult,
    QuoteNotFoundError,
    QuoteExpiredError,
)
from transfercore.infrastructure.tables import ClientBalance, Quotes
from transfercore.domain.money import Money
from datetime import datetime, timezone


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
        )

    @staticmethod
    async def save(session: AsyncSession, quote: QuoteResult) -> QuoteResult:
        expires_str = quote.expires_at.replace("Z", "+00:00")
        expires_at_dt = datetime.fromisoformat(expires_str)
        if expires_at_dt.tzinfo is None:
            expires_at_dt = expires_at_dt.replace(tzinfo=timezone.utc)

        quote_dict = dataclasses.asdict(quote)

        quote_dict.pop("quote_id", None)
        quote_dict.pop("expires_at", None)

        payload_data = jsonable_encoder(quote_dict)

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
