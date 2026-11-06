import json
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder

from transfercore.domain.quote_engine import QuoteEngine, QuoteError
from transfercore.api.quotes import router as quotes_router
from transfercore.domain.storage import InMemoryQuoteStore
from transfercore.domain.money import registry


def create_app(data_dir: Path | str = "data") -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        path = Path(data_dir)
        tariffs = json.loads((path / "tariffs.json").read_text(encoding="utf-8"))
        rates = json.loads((path / "rates.json").read_text(encoding="utf-8"))

        app.state.quote_engine = QuoteEngine(
            tariffs=tariffs, rates=rates, registry=registry
        )
        app.state.quote_store = InMemoryQuoteStore()
        yield

    app = FastAPI(title="Transfer Core", lifespan=lifespan)
    app.include_router(quotes_router)

    @app.exception_handler(QuoteError)
    async def handle_quote_error(request: Request, exc: QuoteError):
        status_code = 400
        if exc.code in ("QUOTE_NOT_FOUND", "UNSUPPORTED_DIRECTION"):
            status_code = 404
        elif exc.code == "QUOTE_EXPIRED":
            status_code = 410
        elif exc.code in ("AMOUNT_BELOW_MIN", "AMOUNT_ABOVE_MAX", "VALIDATION_ERROR"):
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
