import json
import os
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder

from transfercore.domain.quote_engine import QuoteEngine, QuoteError
from transfercore.domain.money import CurrencyRegistry, registry
from transfercore.domain.rates import RateService
from transfercore.api.quotes import router as quotes_router
from transfercore.api.clients import router as clients_router
from transfercore.api.transfers import router as transfers_router
from transfercore.infrastructure.db import AsyncSessionLocal
from transfercore.infrastructure.rate_feed import load_rate_feed
from transfercore.infrastructure.repositories import BalanceRepository


def create_app(data_dir: Path | str | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        path = Path(data_dir) if data_dir is not None else Path(
            os.environ.get("DATA_DIR", "data")
        )
        if not path.exists():
            raise RuntimeError(f"DATA_DIR does not exist: {path}")

        tariffs = json.loads((path / "tariffs.json").read_text(encoding="utf-8"))
        rates = await load_rate_feed(path / "rates.json")

        currencies_file = path / "currencies.json"
        if currencies_file.exists():
            app.state.currency_registry = CurrencyRegistry.from_file(currencies_file)
        else:
            app.state.currency_registry = registry

        app.state.rate_service = RateService(rates)
        app.state.quote_engine = QuoteEngine(
            tariffs=tariffs,
            registry=app.state.currency_registry,
            rate_service=app.state.rate_service,
        )

        app.state.rates_map = app.state.rate_service.rate_map()

        providers_file = path / "providers.json"
        if providers_file.exists():
            app.state.providers = json.loads(providers_file.read_text(encoding="utf-8"))
        else:
            app.state.providers = []

        clients_file = path / "clients.json"
        if clients_file.exists():
            clients = json.loads(clients_file.read_text(encoding="utf-8"))
            app.state.clients = {c["client_id"]: c for c in clients}
            async with AsyncSessionLocal() as session:
                await BalanceRepository.seed_balances(session, clients)
        else:
            app.state.clients = {}

        yield

    app = FastAPI(title="Transfer Core", lifespan=lifespan)
    app.include_router(quotes_router)
    app.include_router(clients_router)
    app.include_router(transfers_router)

    @app.get("/health", tags=["health"])
    async def health():
        return {"status": "ok"}

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
                    "details": {"errors": jsonable_encoder(exc.errors())},
                }
            },
        )

    return app


def main():
    import uvicorn

    uvicorn.run(
        "transfercore.api.app:create_app",
        factory=True,
        host="0.0.0.0",
        port=8080,
    )
