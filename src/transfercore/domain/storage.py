from datetime import datetime, timezone
from typing import Protocol

from transfercore.domain.quote_engine import (
    QuoteResult,
    QuoteNotFoundError,
    QuoteExpiredError,
)


class QuoteStore(Protocol):
    def save(self, quote: QuoteResult) -> None: ...
    def get(self, quote_id: str, now: datetime | None = None) -> QuoteResult: ...


class InMemoryQuoteStore:
    def __init__(self):
        self.quotes_store: dict[str, QuoteResult] = {}

    def save(self, quote: QuoteResult) -> None:
        self.quotes_store[quote.quote_id] = quote

    def get(self, quote_id: str, now: datetime | None = None) -> QuoteResult:
        quote = self.quotes_store.get(quote_id)
        if not quote:
            raise QuoteNotFoundError(f"Quote {quote_id} not found")

        # 1. Получаем текущее время. Если оно передано из теста и "наивное",
        # принудительно считаем его UTC, чтобы тесты не падали с TypeError.
        current_time = now if now is not None else datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)

        # 2. Парсим время истечения.
        # Меняем Z на +00:00 для обратной совместимости со старыми версиями Python
        expires_str = quote.expires_at.replace("Z", "+00:00")
        expires_at = datetime.fromisoformat(expires_str)

        # Подстраховка: если строка изначально была без таймзоны, делаем ее UTC
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        # 3. Теперь обе переменные (current_time и expires_at) точно имеют таймзону UTC
        if current_time >= expires_at:
            raise QuoteExpiredError(f"Quote {quote_id} has expired")

        return quote
