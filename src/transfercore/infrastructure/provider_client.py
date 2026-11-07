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
        timeout_sec: float = 35.0,
    ) -> PayoutResponse:
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
