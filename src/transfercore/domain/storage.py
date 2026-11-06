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


class InMemoreQuoteStore:
    def __init__(self):
        self.quotes_store: dict[str, QuoteResult] = {}

    def save(self, quote: QuoteResult) -> None:
        self.quotes_store[quote.quote_id] = quote

    def get(self, quote_id: str, now: datetime | None = None) -> QuoteResult:
        quote = self.quotes_store.get(quote_id)
        if not quote:
            raise QuoteNotFoundError(f"")

        current_time = now or datetime.now(timezone.utc)

        expires_at = datetime.fromisoformat(quote.expires_at.replace("Z", "+00:00"))

        if current_time >= expires_at:
            raise QuoteExpiredError(f"")

        return quote
