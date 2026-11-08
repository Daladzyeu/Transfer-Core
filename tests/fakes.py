"""Deterministic provider client doubles for worker and webhook tests."""

from dataclasses import dataclass, field

from transfercore.infrastructure.provider_client import PayoutResponse, PayoutStatus


@dataclass
class PayoutCall:
    provider_id: str
    idempotency_key: str
    reference: str
    amount: str
    currency: str
    recipient: dict


@dataclass
class StatusCall:
    kind: str
    provider_id: str
    reference: str


@dataclass
class FakeProviderClient:
    """A scriptable stand-in for ``MockProviderClient``.

    Values in ``responses``, ``find_results`` and ``get_results`` may be a
    concrete object, a callable receiving the recorded call, or a list of
    either (consumed one per call). A provider without a scripted response
    accepts the payout and leaves it processing.
    """

    responses: dict = field(default_factory=dict)
    find_results: dict = field(default_factory=dict)
    get_results: dict = field(default_factory=dict)
    calls: list[PayoutCall] = field(default_factory=list)
    status_calls: list[StatusCall] = field(default_factory=list)

    @staticmethod
    def _resolve(scripted, call, default):
        if isinstance(scripted, list):
            if not scripted:
                return default
            scripted = scripted.pop(0)
        if callable(scripted):
            return scripted(call)
        return scripted if scripted is not None else default

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
        call = PayoutCall(
            provider_id=provider_id,
            idempotency_key=idempotency_key,
            reference=reference,
            amount=amount,
            currency=currency,
            recipient=recipient,
        )
        self.calls.append(call)
        default = PayoutResponse(
            is_success=True,
            status="PROCESSING",
            payout_id=f"po_{provider_id}",
            http_status=201,
        )
        return self._resolve(self.responses.get(provider_id), call, default)

    async def find_payout(
        self, provider_id: str, reference: str, timeout_sec: float = 10.0
    ) -> PayoutStatus:
        call = StatusCall("find", provider_id, reference)
        self.status_calls.append(call)
        default = PayoutStatus(found=False, is_not_found=True, http_status=404)
        return self._resolve(self.find_results.get(provider_id), call, default)

    async def get_payout(
        self, provider_id: str, payout_id: str, timeout_sec: float = 10.0
    ) -> PayoutStatus:
        call = StatusCall("get", provider_id, payout_id)
        self.status_calls.append(call)
        default = PayoutStatus(found=False, is_not_found=True, http_status=404)
        return self._resolve(self.get_results.get(provider_id), call, default)


def accepted(status: str = "PROCESSING", payout_id: str | None = None):
    def factory(call: PayoutCall) -> PayoutResponse:
        return PayoutResponse(
            is_success=True,
            status=status,
            payout_id=payout_id or f"po_{call.provider_id}",
            http_status=201,
        )

    return factory


def declined(code: str = "RECIPIENT_ACCOUNT_INVALID"):
    def factory(call: PayoutCall) -> PayoutResponse:
        return PayoutResponse(
            is_success=False,
            status="DECLINED",
            error_code=code,
            error_message="recipient account is invalid",
            http_status=422,
        )

    return factory


def provider_error(http_status: int = 500):
    def factory(call: PayoutCall) -> PayoutResponse:
        return PayoutResponse(
            is_success=False,
            status="ERROR",
            error_code=f"HTTP_{http_status}",
            error_message="provider error",
            http_status=http_status,
        )

    return factory


def timed_out():
    return PayoutResponse(
        is_success=False,
        status="TIMEOUT",
        error_code="TIMEOUT",
        error_message="provider response timed out",
        http_status=504,
    )


def found(
    status: str,
    payout_id: str | None = None,
    failure_code: str | None = None,
):
    def factory(call: StatusCall) -> PayoutStatus:
        return PayoutStatus(
            found=True,
            status=status,
            payout_id=payout_id or f"po_{call.provider_id}",
            failure_code=failure_code,
            http_status=200,
        )

    return factory


def missing():
    return PayoutStatus(found=False, is_not_found=True, http_status=404)


def lookup_failed():
    return PayoutStatus(found=False, status="ERROR", http_status=0)
