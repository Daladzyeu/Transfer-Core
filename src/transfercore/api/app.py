import json
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from transfercore.domain.quote_engine import QuoteEngine
from transfercore.api.quotes import router as quotes_router
from transfercore.domain.storage import InMemoryQuoteStore


def create_app(data_dir: Path | str = "data") -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        path = Path(data_dir)
        tariffs = json.loads((path / "tariffs.json").read_text(encoding="utf-8"))
        rates = json.loads((path / "rates.json").read_text(encoding="utf-8"))

        app.state.quote_engine = QuoteEngine(tariffs=tariffs, rates=rates)
        app.state.quote_store = InMemoryQuoteStore()
        yield

    app = FastAPI(title="Transfer Core", lifespan=lifespan)
    app.include_router(quotes_router)

    return app


app = create_app()
