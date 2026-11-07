import json
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder

from transfercore.domain.quote_engine import QuoteEngine, QuoteError
from transfercore.domain.money import CurrencyRegistry, registry
from transfercore.api.quotes import router as quotes_router
from transfercore.api.clients import router as clients_router
from transfercore.infrastructure.db import AsyncSessionLocal
from transfercore.infrastructure.repositories import BalanceRepository


def create_app(data_dir: Path | str = "data") -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        path = Path(data_dir)
        tariffs = json.loads((path / "tariffs.json").read_text(encoding="utf-8"))
        rates = json.loads((path / "rates.json").read_text(encoding="utf-8"))

        currencies_file = path / "currencies.json"
        if currencies_file.exists():
            app.state.currency_registry = CurrencyRegistry.from_file(currencies_file)
        else:
            app.state.currency_registry = registry

        app.state.quote_engine = QuoteEngine(
            tariffs=tariffs, rates=rates, registry=app.state.currency_registry
        )

        clients_file = path / "clients.json"
        if clients_file.exists():
            clients = json.loads(clients_file.read_text(encoding="utf-8"))
            app.state.clients = {c["client_id"]: c for c in clients}
            try:
                async with AsyncSessionLocal() as session:
                    await BalanceRepository.seed_balances(session, clients)
            except Exception:
                pass
        else:
            app.state.clients = {}

        yield

    app = FastAPI(title="Transfer Core", lifespan=lifespan)
    app.include_router(quotes_router)
    app.include_router(clients_router)

    @app.exception_handler(QuoteError)
    async def handle_quote_error(request: Request, exc: QuoteError):
        status_code = 400
        if exc.code in ("QUOTE_NOT_FOUND", "UNSUPPORTED_DIRECTION", "CLIENT_NOT_FOUND"):
            status_code = 404
        elif exc.code == "QUOTE_EXPIRED":
            status_code = 410
        elif exc.code in (
            "AMOUNT_BELOW_MIN",
            "AMOUNT_ABOVE_MAX",
            "VALIDATION_ERROR",
            "INSUFFICIENT_FUNDS",
            "DAILY_LIMIT_EXCEEDED",
        ):
            status_code = 422

        return JSONResponse(
            status_code=status_code,
            content=jsonable_encoder(
                {
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "details": exc.details,
                    }
                }
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Invalid request format",
                    "details": {"errors": exc.errors()},
                }
            },
        )

    return app


def main():
    app = create_app()
