import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from transfercore.api.app import create_app
from transfercore.infrastructure.db import AsyncSessionLocal

BASE_REQUEST = {
    "source_country": "RU",
    "source_currency": "RUB",
    "target_country": "TH",
    "target_currency": "THB",
    "payout_method": "BANK_ACCOUNT",
    "amount": "5000.00",
    "amount_type": "SOURCE",
}

EXAMPLES = sorted(Path("examples").glob("*.json"))


@asynccontextmanager
async def quote_client():
    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client


@pytest.mark.anyio
@pytest.mark.parametrize("example_path", EXAMPLES, ids=lambda path: path.stem)
async def test_examples_over_http(example_path):
    data = json.loads(example_path.read_text(encoding="utf-8"))
    expected = data["response"]

    async with quote_client() as client:
        response = await client.post("/quotes", json=data["request"])

        if "error" in expected["body"]:
            code = expected["body"]["error"]["code"]
            expected_status = 404 if code == "UNSUPPORTED_DIRECTION" else 422
            assert response.status_code == expected_status
            assert response.json()["error"]["code"] == code
            return

        assert response.status_code == 201
        body = response.json()
        for field in (
            "direction_id",
            "source_amount",
            "fee",
            "amount_to_convert",
            "rate",
            "target_amount",
        ):
            assert body[field] == expected["body"][field]

        assert isinstance(body["breakdown"], list)
        assert body["breakdown"]

        stored = await client.get(f"/quotes/{body['quote_id']}")
        assert stored.status_code == 200
        assert stored.json() == body


@pytest.mark.anyio
@pytest.mark.parametrize(
    "overrides",
    [
        {"amount": "abc"},
        {"amount": "100.5"},
        {"amount": "100.555"},
        {"amount": "100"},
        {"amount": 10000},
        {"amount": None},
        {"amount_type": "BOGUS"},
        {"amount_type": "source"},
        {"source_currency": "XXX"},
        {"target_currency": "XXX"},
        {"source_currency": "rub"},
        {"source_country": "RUS"},
        {"target_country": "th"},
    ],
)
async def test_invalid_requests_return_validation_error(overrides):
    async with quote_client() as client:
        response = await client.post(
            "/quotes", json={**BASE_REQUEST, **overrides}
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.anyio
async def test_missing_field_returns_validation_error():
    payload = dict(BASE_REQUEST)
    payload.pop("amount")

    async with quote_client() as client:
        response = await client.post("/quotes", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.anyio
async def test_target_mode_limits_over_http():
    async with quote_client() as client:
        response = await client.post(
            "/quotes",
            json={
                **BASE_REQUEST,
                "amount": "200000.00",
                "amount_type": "TARGET",
            },
        )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "AMOUNT_ABOVE_MAX"
    assert error["details"]["max"] == "300000.00"
    assert error["details"]["currency"] == "RUB"


@pytest.mark.anyio
async def test_target_mode_fee_in_target_over_http():
    async with quote_client() as client:
        response = await client.post(
            "/quotes",
            json={
                **BASE_REQUEST,
                "target_country": "VN",
                "target_currency": "VND",
                "amount": "500000",
                "amount_type": "TARGET",
            },
        )

    assert response.status_code == 201
    body = response.json()
    assert body["source_amount"] == {"amount": "1766.67", "currency": "RUB"}
    assert body["fee"] == {"amount": "30000", "currency": "VND"}
    assert body["amount_to_convert"] == {
        "amount": "1766.67",
        "currency": "RUB",
    }
    assert body["target_amount"] == {"amount": "500000", "currency": "VND"}


@pytest.mark.anyio
async def test_cross_rate_over_http():
    async with quote_client() as client:
        response = await client.post(
            "/quotes",
            json={
                **BASE_REQUEST,
                "target_country": "PH",
                "target_currency": "PHP",
                "payout_method": "CASH_PICKUP",
                "amount": "10000.00",
            },
        )

    assert response.status_code == 201
    body = response.json()
    assert body["rate"] == "0.6960"
    assert body["target_amount"] == {"amount": "6786.00", "currency": "PHP"}


@pytest.mark.anyio
async def test_stale_rate_over_http():
    async with quote_client() as client:
        response = await client.post(
            "/quotes",
            json={
                **BASE_REQUEST,
                "target_country": "AE",
                "target_currency": "AED",
                "amount": "10000.00",
            },
        )

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "RATE_UNAVAILABLE"
    assert error["details"]["reason"] == "stale"


@pytest.mark.anyio
async def test_quote_payload_stores_money_as_strings():
    async with quote_client() as client:
        response = await client.post("/quotes", json=BASE_REQUEST)
        assert response.status_code == 201
        quote_id = response.json()["quote_id"]

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT payload FROM quotes WHERE quote_id = :quote_id"),
            {"quote_id": quote_id},
        )
        payload = result.scalar_one()

    for field in ("source_amount", "fee", "amount_to_convert", "target_amount"):
        assert isinstance(payload[field]["amount"], str)
        assert isinstance(payload[field]["currency"], str)

    assert isinstance(payload["rate"], str)
    assert isinstance(payload["breakdown"], list)
    assert all(isinstance(step, str) for step in payload["breakdown"])
    assert isinstance(payload["rate_updated_at"], str)


@pytest.mark.anyio
async def test_unknown_quote_returns_quote_not_found():
    async with quote_client() as client:
        response = await client.get("/quotes/q_does_not_exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "QUOTE_NOT_FOUND"
