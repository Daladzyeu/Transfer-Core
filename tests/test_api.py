from decimal import Decimal
import pytest
from httpx import AsyncClient, ASGITransport
from transfercore.api.app import create_app
from transfercore.infrastructure.db import AsyncSessionLocal
from transfercore.infrastructure.repositories import BalanceRepository
from transfercore.domain.quote_engine import InsufficientFundsError


@pytest.mark.anyio
async def test_post_and_get_quote_via_http():
    app = create_app()
    async with app.router.lifespan_context(app):
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


@pytest.mark.anyio
async def test_get_client_balance():
    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Test existing seeded client
            resp = await client.get("/clients/c_001/balance")
            assert resp.status_code == 200
            data = resp.json()
            assert data["client_id"] == "c_001"
            assert "RUB" in data["balances"]
            assert Decimal(data["balances"]["RUB"]["available"]) > Decimal("0")
            assert data["balances"]["RUB"]["reserved"] == "0.00"

            # Test unknown client
            not_found_resp = await client.get("/clients/c_unknown_999/balance")
            assert not_found_resp.status_code == 404
            err_data = not_found_resp.json()
            assert err_data["error"]["code"] == "CLIENT_NOT_FOUND"


@pytest.mark.anyio
async def test_balance_hold_and_release_invariants():
    """Verify available + reserved invariant during hold and release."""
    async with AsyncSessionLocal() as session:
        # Check starting state for c_001
        async with session.begin():
            balances = await BalanceRepository.get_balances_by_client(session, "c_001")
            rub_balance = next(b for b in balances if b.currency == "RUB")
            start_available = rub_balance.available
            start_reserved = rub_balance.reserved

        hold_amount = Decimal("1000.00")

        # 1. Place hold
        async with session.begin():
            await BalanceRepository.hold_funds(session, "c_001", "RUB", hold_amount)

        async with session.begin():
            b1 = await BalanceRepository.get_balance_for_update(session, "c_001", "RUB")
            assert b1.available == start_available - hold_amount
            assert b1.reserved == start_reserved + hold_amount
            assert b1.available + b1.reserved == start_available + start_reserved

        # 2. Release hold (simulating failed transfer refund)
        async with session.begin():
            await BalanceRepository.release_hold(session, "c_001", "RUB", hold_amount)

        async with session.begin():
            b2 = await BalanceRepository.get_balance_for_update(session, "c_001", "RUB")
            assert b2.available == start_available
            assert b2.reserved == start_reserved

        # 3. Test holding more than available raises InsufficientFundsError
        with pytest.raises(InsufficientFundsError):
            async with session.begin():
                await BalanceRepository.hold_funds(
                    session, "c_001", "RUB", Decimal("99999999.00")
                )


@pytest.mark.anyio
async def test_transfer_creation_and_idempotency_flow():
    from transfercore.infrastructure.transfer_worker import TransferWorker
    from fakes import FakeProviderClient

    app = create_app(start_worker=False)
    fake = FakeProviderClient()
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # 1. Create a Quote first
            quote_resp = await client.post(
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
            assert quote_resp.status_code == 201
            quote_id = quote_resp.json()["quote_id"]

            transfer_body = {
                "quote_id": quote_id,
                "client_id": "c_001",
                "recipient": {
                    "name": "Somchai Jaidee",
                    "account": "1234567890",
                    "bank_code": "BKKB",
                },
            }

            # 2. Submit transfer with Idempotency-Key. The API accepts the
            #    request and leaves processing to the background worker.
            idemp_key = f"key-{quote_id}"
            resp1 = await client.post(
                "/transfers",
                json=transfer_body,
                headers={"Idempotency-Key": idemp_key},
            )
            assert resp1.status_code == 202
            data1 = resp1.json()
            transfer_id = data1["transfer_id"]
            assert transfer_id.startswith("tr_")
            assert data1["status"] == "PENDING"
            assert data1["source_amount"]["amount"] == "5000.00"
            assert fake.calls == []

            # 3. Submit identical transfer with the SAME Idempotency-Key
            resp2 = await client.post(
                "/transfers",
                json=transfer_body,
                headers={"Idempotency-Key": idemp_key},
            )
            assert resp2.status_code == 202
            assert resp2.headers["Idempotency-Replayed"] == "true"
            data2 = resp2.json()
            # Must return the EXACT same transfer!
            assert data2["transfer_id"] == transfer_id

            # 4. The worker dispatches the pending transfer.
            worker = TransferWorker(app.state, provider_client=fake)
            assert await worker.process_pending() == 1
            assert [call.provider_id for call in fake.calls] == ["prov_alpha"]

            # 5. GET /transfers/{transfer_id}
            get_resp = await client.get(f"/transfers/{transfer_id}")
            assert get_resp.status_code == 200
            get_data = get_resp.json()
            assert get_data["transfer_id"] == transfer_id
            assert get_data["status"] == "PROCESSING"
            assert get_data["provider_id"] == "prov_alpha"
            assert len(get_data["attempts"]) == 1
            assert [t["to"] for t in get_data["transitions"]] == [
                "PENDING",
                "PROCESSING",
            ]

            # 6. Missing Idempotency-Key header raises 422
            err_resp = await client.post("/transfers", json=transfer_body)
            assert err_resp.status_code == 422
            assert err_resp.json()["error"]["code"] == "VALIDATION_ERROR"

            # 7. Unknown transfer is a 404
            missing = await client.get("/transfers/tr_missing")
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "TRANSFER_NOT_FOUND"
