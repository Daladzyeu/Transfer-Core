from pydantic import BaseModel, Field


class QuoteCreateRequest(BaseModel):
    source_country: str
    source_currency: str
    target_country: str
    target_currency: str
    payout_method: str
    amount: str
    amount_type: str


class MoneyModel(BaseModel):
    amount: str
    currency: str


class QuoteResponse(BaseModel):
    quote_id: str
    direction_id: str
    source_amount: MoneyModel
    fee: MoneyModel
    amount_to_convert: MoneyModel
    rate: str
    target_amount: MoneyModel
    breakdown: list[str] | None = None
    expires_at: str
