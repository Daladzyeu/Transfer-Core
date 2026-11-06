import pytest
from httpx import AsyncClient, ASGITransport
from transfercore.api.app import create_app


@pytest.mark.anyio
async def test_post_and_get_quote_via_http():
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # 1. POST /quotes
        resp = await client.post(
            "/quotes",
            json={
                "source_country": "RU",
                "source_currency": "RUB",
                "target_country": "TH",
                "target_currency": "THB",
                "payout_method": "BANK_ACCOUNT",
                "amount": "5000.00",
                "amount_type": "SOURCE",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        quote_id = data["quote_id"]
        assert data["target_amount"]["amount"] == "2068.50"

        # 2. GET /quotes/{quote_id}
        get_resp = await client.get(f"/quotes/{quote_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["quote_id"] == quote_id
