import dataclasses
from decimal import Decimal

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from transfercore.domain.quote_engine import QuoteResult
from transfercore.infrastructure.tables import Quotes
from transfercore.domain.money import Money
from datetime import datetime, timezone

from transfercore.domain.quote_engine import (
    QuoteResult,
    QuoteNotFoundError,
    QuoteExpiredError,
)


class QuoteRepository:
    @staticmethod
    async def get_by_id(session: AsyncSession, quote_id: str) -> QuoteResult:
        stmt = select(Quotes).where(Quotes.quote_id == quote_id)
        result = await session.execute(stmt)

        # 1. Извлекаем ORM-объект. Вернет None, если записи нет
        db_quote = result.scalar_one_or_none()

        if not db_quote:
            raise QuoteNotFoundError(f"Quote {quote_id} not found")

        # 2. Проверяем срок действия.
        # db_quote.expires_at — это УЖЕ объект datetime.
        expires_at = db_quote.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        current_time = datetime.now(timezone.utc)

        if current_time >= expires_at:
            raise QuoteExpiredError(f"Quote {quote_id} has expired")

        # 3. Восстанавливаем датакласс QuoteResult из JSONB
        payload = db_quote.payload

        # Форматируем дату обратно в строку ISO с буквой Z, как ожидает ваш домен
        expires_str = expires_at.isoformat().replace("+00:00", "Z")

        def parse_money(data: dict) -> Money:
            return Money(amount=Decimal(str(data["amount"])), currency=data["currency"])

        # ВАЖНО: Замените инициализацию Money в зависимости от того,
        # как выглядит конструктор вашего класса Money.
        # Если JSON сохранил их как словари {"amount": 100, "currency": "RUB"},
        # то распаковка через ** подойдет.
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

        # 2. Подготавливаем payload для JSONB
        # Превращаем датакласс в словарь
        quote_dict = dataclasses.asdict(quote)

        # Удаляем поля, которые лежат в отдельных колонках БД, чтобы не дублировать данные
        quote_dict.pop("quote_id", None)
        quote_dict.pop("expires_at", None)

        # jsonable_encoder безопасно превратит Decimal в строки/числа,
        # а ваш кастомный Money — в словарь (например, {"amount": "100.00", "currency": "RUB"})
        payload_data = jsonable_encoder(quote_dict)

        # 3. Собираем объект базы данных
        db_quote = Quotes(
            quote_id=quote.quote_id, payload=payload_data, expires_at=expires_at_dt
        )
        session.add(db_quote)
        await session.commit()
        await session.refresh(db_quote)
        return quote
