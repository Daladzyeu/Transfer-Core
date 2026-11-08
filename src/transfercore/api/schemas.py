from typing import Literal

from pydantic import BaseModel, Field


class QuoteCreateRequest(BaseModel):
    source_country: str = Field(..., pattern=r"^[A-Z]{2}$")
    source_currency: str = Field(..., pattern=r"^[A-Z]{3}$")
    target_country: str = Field(..., pattern=r"^[A-Z]{2}$")
    target_currency: str = Field(..., pattern=r"^[A-Z]{3}$")
    payout_method: str
    amount: str = Field(..., strict=True)
    amount_type: Literal["SOURCE", "TARGET"]


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
