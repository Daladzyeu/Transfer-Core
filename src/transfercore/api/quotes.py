from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from transfercore.api.schemas import QuoteCreateRequest, QuoteResponse, MoneyModel
from transfercore.domain.quote_engine import QuoteRequest, QuoteResult
from transfercore.infrastructure.db import get_db
from transfercore.infrastructure.repositories import QuoteRepository

router = APIRouter(prefix="/quotes", tags=["quotes"])


def quote_to_response(q: QuoteResult) -> QuoteResponse:

    return QuoteResponse(
        quote_id=q.quote_id,
        direction_id=q.direction_id,
        source_amount=MoneyModel(
            amount=q.source_amount.to_str(), currency=q.source_amount.currency
        ),
        fee=MoneyModel(amount=q.fee.to_str(), currency=q.fee.currency),
        amount_to_convert=MoneyModel(
            amount=q.amount_to_convert.to_str(), currency=q.amount_to_convert.currency
        ),
        rate=str(q.rate),
        target_amount=MoneyModel(
            amount=q.target_amount.to_str(), currency=q.target_amount.currency
        ),
        breakdown=q.breakdown,
        expires_at=q.expires_at,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_quote(
    body: QuoteCreateRequest, request: Request, session: AsyncSession = Depends(get_db)
):
    engine = request.app.state.quote_engine

    domain_request = QuoteRequest(
        source_country=body.source_country,
        source_currency=body.source_currency,
        target_country=body.target_country,
        target_currency=body.target_currency,
        payout_method=body.payout_method,
        amount=body.amount,
        amount_type=body.amount_type,
    )

    quote = engine.calculate(domain_request)
    await QuoteRepository.save(session=session, quote=quote)

    return quote_to_response(quote)


@router.get("/{quote_id}", status_code=status.HTTP_200_OK)
async def get_quote(
    quote_id: str, request: Request, session: AsyncSession = Depends(get_db)
):
    quote = await QuoteRepository.get_by_id(session=session, quote_id=quote_id)
    return quote_to_response(quote)
