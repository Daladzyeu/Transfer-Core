import json
import logging
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

from transfercore.infrastructure.provider_client import get_mock_provider_url

log = logging.getLogger(__name__)

_SNAPSHOT_VALUES = {"snapshot", "off", "none", "static"}


def _default_feed_url() -> str:
    return f"{get_mock_provider_url()}/rates"


def _valid_feed(data) -> bool:
    if not isinstance(data, list) or not data:
        return False

    for item in data:
        if not isinstance(item, dict):
            return False
        for key in ("base", "quote", "rate", "updated_at"):
            if not isinstance(item.get(key), str) or not item[key]:
                return False
        try:
            if Decimal(item["rate"]) <= 0:
                return False
        except InvalidOperation:
            return False

    return True


async def load_rate_feed(
    snapshot_path: Path | str,
    *,
    url: str | None = None,
    timeout: float = 2.0,
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    """Load live rates from the mock provider, falling back to the snapshot.

    ``RATES_URL=snapshot`` (or an explicit ``url="snapshot"``) forces the
    bundled ``rates.json`` snapshot, which keeps tests deterministic.
    """
    snapshot: list[dict] = []
    path = Path(snapshot_path)
    if path.exists():
        snapshot = json.loads(path.read_text(encoding="utf-8"))

    if url is not None:
        if url.strip().lower() in _SNAPSHOT_VALUES:
            return snapshot
        feed_url = url.strip()
    else:
        configured = os.environ.get("RATES_URL", "").strip()
        if configured.lower() in _SNAPSHOT_VALUES:
            return snapshot
        feed_url = configured or _default_feed_url()

    try:
        if client is None:
            async with httpx.AsyncClient(timeout=timeout) as http_client:
                response = await http_client.get(feed_url)
        else:
            response = await client.get(feed_url)

        response.raise_for_status()
        data = response.json()
        if _valid_feed(data):
            log.info("Loaded %d live rates from %s", len(data), feed_url)
            return data
        log.warning("Invalid live rate feed from %s; using snapshot", feed_url)
    except Exception as exc:
        log.warning(
            "Could not load live rates from %s (%s); using snapshot",
            feed_url,
            exc,
        )

    return snapshot
