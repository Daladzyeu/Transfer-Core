import asyncio
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import text

from transfercore.infrastructure.db import AsyncSessionLocal, engine
from transfercore.infrastructure.repositories import BalanceRepository

# Keep tests deterministic: use the bundled rates snapshot instead of the
# running mock provider. The live-feed path has its own unit test.
os.environ["RATES_URL"] = "snapshot"


async def _reset_database() -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("TRUNCATE TABLE transfers, quotes, balances RESTART IDENTITY CASCADE")
        )

    data_dir = Path(os.environ.get("DATA_DIR", "data"))
    clients = json.loads((data_dir / "clients.json").read_text(encoding="utf-8"))
    async with AsyncSessionLocal() as session:
        await BalanceRepository.seed_balances(session, clients)


@pytest.fixture(autouse=True)
def clean_database():
    """Give every test a deterministic database state.

    The test database is migrated once by the test entrypoint. Each test gets
    empty business tables and the balances from data/clients.json.
    """
    asyncio.run(_reset_database())
    yield
