from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from transfercore.domain.money import Money, registry
from transfercore.infrastructure.db import get_db
from transfercore.infrastructure.repositories import BalanceRepository

router = APIRouter(prefix="/clients", tags=["clients"])


@router.get("/{client_id}/balance", status_code=status.HTTP_200_OK)
async def get_client_balance(
    client_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    balances = await BalanceRepository.get_balances_by_client(session, client_id)
    if not balances:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "error": {
                    "code": "CLIENT_NOT_FOUND",
                    "message": f"Client {client_id} not found",
                }
            },
        )

    reg = getattr(request.app.state, "currency_registry", registry)
    balances_dict = {}
    for b in balances:
        avail_str = Money.from_decimal(b.available, b.currency, reg).to_str()
        res_str = Money.from_decimal(b.reserved, b.currency, reg).to_str()
        balances_dict[b.currency] = {
            "available": avail_str,
            "reserved": res_str,
        }

    return {
        "client_id": client_id,
        "balances": balances_dict,
    }
