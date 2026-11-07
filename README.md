# Transfer Core

Backend for the Northwind Transfer test assignment: quote a currency
conversion, hold client funds, execute a payout through an external provider,
and survive provider failures.

## Requirements

- Docker and Docker Compose

## Run

```bash
docker compose up --build
```

The API listens on `http://localhost:8080`, the mock provider on
`http://localhost:9000`. Database migrations run automatically when the API
container starts.

Check that the service is up:

```bash
curl http://localhost:8080/health
```

Create a quote:

```bash
curl -s -X POST http://localhost:8080/quotes \
  -H 'Content-Type: application/json' \
  -d '{
    "source_country": "RU",
    "source_currency": "RUB",
    "target_country": "TH",
    "target_currency": "THB",
    "payout_method": "BANK_ACCOUNT",
    "amount": "10000.00",
    "amount_type": "SOURCE"
  }'
```

## Tests

One command, using a disposable PostgreSQL database:

```bash
docker compose --profile test run --rm --build test
```

The test service runs `alembic upgrade head` against its own database and then
runs `pytest`. Each test starts with empty business tables and balances seeded
from `data/clients.json`.

For local development without Docker:

```bash
uv sync --all-extras
export DATABASE_URL=postgresql://transfer:transfer@localhost:5432/transfer
uv run alembic upgrade head
uv run pytest
```

Do not point the local test run at a database whose data you want to keep:
the test fixture truncates business tables.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://transfer:transfer@localhost:5432/transfer` | PostgreSQL connection URL |
| `DATA_DIR` | `data` | Directory containing `currencies.json`, `tariffs.json`, `rates.json`, `providers.json`, `clients.json` |
| `MOCK_PROVIDER_URL` | `http://mock-provider:9000` | Mock provider base URL |
| `CALLBACK_BASE_URL` | `http://api:8080` | Base URL used in provider `callback_url` |
| `PROVIDER_WEBHOOK_SECRET` | `local-dev-webhook-secret` | Shared secret for webhook signatures |

The values above are local-development defaults, not production secrets.

## Chaos mode

The mock provider can run in chaos mode:

```bash
MOCK_MODE=chaos MOCK_SEED=1 docker compose up --build
python scripts/chaos_run.py --transfers 300
```

Chaos/reconciliation support is still being completed; see `LIMITS.md`.

## Project layout

```text
src/transfercore/
  api/              HTTP routes and request/response models
  domain/           money, quote calculation, provider routing
  infrastructure/   database, repositories, provider HTTP client
migrations/         Alembic migrations
mock-provider/      provider emulator supplied with the assignment
scripts/            chaos/self-check scripts
tests/              unit and integration tests
data/               assignment data files
```

## Documentation

- `DECISIONS.md` — design decisions and state machine
- `ASSUMPTIONS.md` — ambiguities and chosen resolutions
- `LIMITS.md` — what is not implemented and what will break
