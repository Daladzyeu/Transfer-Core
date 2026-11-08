import os
import socket
from dataclasses import dataclass
import httpx


@dataclass
class PayoutResponse:
    is_success: bool
    status: str  # "PROCESSING", "DECLINED", "ERROR", "TIMEOUT"
    payout_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    http_status: int = 0


@dataclass
class PayoutStatus:
    found: bool
    status: str | None = None
    payout_id: str | None = None
    failure_code: str | None = None
    http_status: int = 0
    is_not_found: bool = False


def get_mock_provider_url() -> str:
    url = os.environ.get("MOCK_PROVIDER_URL", "http://mock-provider:9000")
    if "mock-provider" in url:
        try:
            socket.gethostbyname("mock-provider")
        except socket.gaierror:
            url = url.replace("mock-provider", "localhost")
    return url.rstrip("/")


class MockProviderClient:
    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or get_mock_provider_url()).rstrip("/")

    async def create_payout(
        self,
        provider_id: str,
        idempotency_key: str,
        reference: str,
        amount: str,
        currency: str,
        recipient: dict,
        callback_url: str | None = None,
        timeout_sec: float | None = None,
    ) -> PayoutResponse:
        if timeout_sec is None:
            timeout_sec = float(os.environ.get("PROVIDER_TIMEOUT_SEC", "35"))
        url = f"{self.base_url}/{provider_id}/payouts"
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        }
        callback_base = os.environ.get(
            "CALLBACK_BASE_URL", "http://api:8080"
        ).rstrip("/")
        cb_url = callback_url or f"{callback_base}/webhooks/{provider_id}"
        payload = {
            "reference": reference,
            "amount": amount,
            "currency": currency,
            "recipient": recipient,
            "callback_url": cb_url,
        }

        try:
            async with httpx.AsyncClient(timeout=timeout_sec) as client:
                resp = await client.post(url, json=payload, headers=headers)
                data = resp.json() if resp.content else {}

                if resp.status_code == 201:
                    return PayoutResponse(
                        is_success=True,
                        status=data.get("status", "PROCESSING"),
                        payout_id=data.get("payout_id"),
                        http_status=resp.status_code,
                    )
                elif resp.status_code == 422:
                    return PayoutResponse(
                        is_success=False,
                        status="DECLINED",
                        error_code=data.get("code", "DECLINED"),
                        error_message=data.get("message", "Payout declined by provider"),
                        http_status=resp.status_code,
                    )
                else:
                    return PayoutResponse(
                        is_success=False,
                        status="ERROR",
                        error_code=f"HTTP_{resp.status_code}",
                        error_message=str(data),
                        http_status=resp.status_code,
                    )
        except httpx.TimeoutException:
            # Timeout: Provider might be processing (Section 6)
            return PayoutResponse(
                is_success=False,
                status="TIMEOUT",
                error_code="TIMEOUT",
                error_message="Provider response timed out",
                http_status=504,
            )
        except Exception as exc:
            return PayoutResponse(
                is_success=False,
                status="ERROR",
                error_code="TRANSPORT_ERROR",
                error_message=str(exc),
                http_status=0,
            )

    async def get_payout(
        self,
        provider_id: str,
        payout_id: str,
        timeout_sec: float = 10.0,
    ) -> PayoutStatus:
        url = f"{self.base_url}/{provider_id}/payouts/{payout_id}"
        return await self._get_status(url, timeout_sec)

    async def find_payout(
        self,
        provider_id: str,
        reference: str,
        timeout_sec: float = 10.0,
    ) -> PayoutStatus:
        url = f"{self.base_url}/{provider_id}/payouts"
        return await self._get_status(
            url, timeout_sec, params={"reference": reference}
        )

    async def _get_status(
        self,
        url: str,
        timeout_sec: float,
        params: dict | None = None,
    ) -> PayoutStatus:
        try:
            async with httpx.AsyncClient(timeout=timeout_sec) as client:
                resp = await client.get(url, params=params)
                if resp.status_code == 404:
                    return PayoutStatus(
                        found=False,
                        is_not_found=True,
                        http_status=resp.status_code,
                    )
                if resp.status_code != 200:
                    return PayoutStatus(
                        found=False,
                        status="ERROR",
                        http_status=resp.status_code,
                    )
                data = resp.json()
                return PayoutStatus(
                    found=True,
                    status=data.get("status"),
                    payout_id=data.get("payout_id"),
                    failure_code=data.get("failure_code"),
                    http_status=resp.status_code,
                )
        except Exception:
            return PayoutStatus(
                found=False,
                status="ERROR",
                http_status=0,
            )
