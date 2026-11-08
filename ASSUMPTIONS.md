# Assumptions and chosen resolutions

This file records the ambiguities found in the assignment and the decisions
made for them. Phase 1 (quote calculation) and Phase 2 (execution, provider
routing and reliability) are both covered.

## Money and rounding

- **One rounding rule.** All monetary values are `Decimal`; floats are never
  used for calculation or persistence. Rounding is `ROUND_HALF_UP`, applied in
  one place (`Money.from_decimal` / `Money.to_str`).
- **Minor units come from `currencies.json`.** An amount is always rounded to
  the minor units of its own currency (2 for RUB/THB/USD/EUR, 0 for JPY/VND,
  3 for KWD).
- **The fee is rounded first.** The fee is computed from the exact amount,
  then rounded to the minor units of the currency it is charged in
  (`fee.charged_in`). The rest of the calculation uses that rounded fee, so
  the numbers shown in the response add up exactly.
- **Amounts are rounded to their own currency.** `amount_to_convert` and
  `target_amount` are rounded independently to their currencies' minor units.
  In `TARGET` mode the requested target amount is kept exactly as requested.
- **Limits use the gross source amount.** The direction limits are checked
  against the full amount debited from the client (amount to convert + fee),
  not against the requested amount. In `TARGET` mode this means the target
  amount is converted back to the source currency before the limit check.
  Limits are inclusive: `min <= amount <= max`.
- If a limit is expressed in a currency other than the source currency, the
  gross source amount is converted through the same rate service before
  comparison.

### Fee matrix

`amount_type` and `fee.charged_in` form four cases:

| `amount_type` | `fee.charged_in` | Calculation |
|---|---|---|
| `SOURCE` | `SOURCE` | `source = requested`; `fee = max(fixed + percent * source, min)`; `amount_to_convert = source - fee`; `target = amount_to_convert * rate` |
| `SOURCE` | `TARGET` | `source = requested`; `gross_target = source * rate`; `fee = max(fixed + percent * gross_target, min)`; `target = gross_target - fee` |
| `TARGET` | `SOURCE` | Solve `source = target / rate + fee(source)` for the percent part; if the resulting fee is below `min`, use `min`; `amount_to_convert = target / rate`; `source = amount_to_convert + fee` |
| `TARGET` | `TARGET` | Solve `gross_target = target + fee(gross_target)`; if the fee is below `min`, use `min`; `gross_target = target + fee`; `amount_to_convert = gross_target / rate`; `source = amount_to_convert` |

The `SOURCE`/`SOURCE` and `TARGET`/`SOURCE` cases round the fee and the
source amount separately and then define `source = amount_to_convert + fee`,
so a `SOURCE` quote and the equivalent `TARGET` quote agree exactly.

## Exchange rates

- **Feed source.** At startup the service tries the live mock provider
  (`GET /rates`). If it is unreachable or returns an unusable payload, the
  bundled `data/rates.json` snapshot is used. `RATES_URL=snapshot` forces the
  snapshot (used by tests).
- **Freshness window.** A rate is fresh when its `updated_at` is at most
  6 hours older than the newest timestamp in the feed. Freshness is measured
  against the feed, not against wall-clock time, because the snapshot is
  intentionally historical; this also matches the mock provider's own
  stale-rate semantics.
- **Lookup order.** Direct pair, then the inverse pair (rate = `1 / rate`),
  then one-hop cross rates through intermediate currencies in sorted order.
  Inverse and cross rates are quantized to 10 decimal places with at least
  4 significant decimal places kept for readability.
- **Unusable rates.** Missing or stale rates produce `RATE_UNAVAILABLE` with
  `details.reason` set to `missing` or `stale`. There is no silent fallback
  to an older rate.
- **Identity.** A source currency equal to the target currency uses rate `1`
  without consulting the feed.
- The quote stores the rate actually used and `rate_updated_at` (the oldest
  timestamp contributing to a cross rate) for auditability.

## Validation

- Country codes must be two uppercase ASCII letters; currency codes must be
  three uppercase ASCII letters and must exist in `currencies.json`.
- `amount` must be a JSON string with exactly the number of decimal digits
  required by the amount's currency (`SOURCE` → source currency,
  `TARGET` → target currency). Numeric JSON values are rejected.
- `amount_type` must be `SOURCE` or `TARGET`.
- A syntactically valid zero amount is not a format error: for the assignment
  tariffs (all have a positive minimum) it is handled by the normal limit
  check and returns `AMOUNT_BELOW_MIN`. A hypothetical tariff with a zero
  minimum would reject it as a non-positive calculation with
  `VALIDATION_ERROR`.
- `payout_method` is matched exactly against the tariff.

## Quote lifetime and status codes

- `expires_at` is calculated from the moment the quote is created using the
  direction's `quote_ttl_sec`. The quote and its full calculation are
  persisted; `GET /quotes/{id}` returns the stored result.
- An expired quote returns `QUOTE_EXPIRED` (`410 Gone`).
- HTTP mappings:
  - `VALIDATION_ERROR`, `AMOUNT_BELOW_MIN`, `AMOUNT_ABOVE_MAX`,
    `INSUFFICIENT_FUNDS`, `DAILY_LIMIT_EXCEEDED` → `422`
  - `UNSUPPORTED_DIRECTION`, `QUOTE_NOT_FOUND` → `404`
  - `QUOTE_EXPIRED` → `410`
  - `RATE_UNAVAILABLE` → `400`
- Request-schema failures (missing field, wrong JSON type, bad pattern,
  invalid `amount_type`) are also returned as `VALIDATION_ERROR` with `422`.

## Determinism

The same request against the same tariff and rate data always produces the
same amounts, fee, rate and breakdown. Only `quote_id` and `expires_at`
change between two calculations.

## Phase 2 — transfer execution

### Creating a transfer

- A transfer can only be created from a **persisted, unexpired quote**.
  `QuoteRepository.get_by_id` raises `QUOTE_NOT_FOUND` (`404`) or
  `QUOTE_EXPIRED` (`410`); the transfer endpoint does not accept raw
  direction/amount data.
- The quote’s `source_amount`, `fee` and `target_amount` are copied into the
  transfer. The quote price is the transfer price; a later rate change does
  not alter an existing transfer.
- `client_id` must exist in `data/clients.json`; otherwise
  `CLIENT_NOT_FOUND` (`404`).
- The recipient is a snapshot of `name`, `account` and `bank_code`. Only
  non-empty strings are required; the provider is the authority on whether
  the account is valid.
- `POST /transfers` returns `202 Accepted`, not `201`. The body is the
  transfer resource with `status: PENDING`. The request is durable and the
  funds hold is committed before the response is sent, but no provider has
  been called yet.
- There is no transfer list endpoint. The client is expected to keep the
  `transfer_id` from the creation response (or use the idempotency key).

### State semantics

- `PENDING` = queued; no provider has accepted a payout yet. It is not a
  failure and does not mean the provider was contacted.
- `PROCESSING` = a provider accepted a payout; `provider_id` and
  `provider_payout_id` identify it.
- `NEEDS_REVIEW` = the provider outcome is unknown. Funds remain reserved.
  It is explicitly non-terminal and is resolved by reconciliation or a
  webhook. It is reported as a distinct status, not hidden as `PROCESSING`.
- `COMPLETED` and `FAILED` are terminal and sticky. A late or contradictory
  event cannot reopen them or move funds twice.
- The transition history is append-only JSON on the transfer, including
  `from`, `to`, `timestamp`, `source` (`client`, `worker`, `provider`,
  `reconciliation`) and an optional reason. It is returned by
  `GET /transfers/{id}` for audit.

### Idempotency

- `Idempotency-Key` is required; a missing header is a `VALIDATION_ERROR`
  (`422`).
- The key is global, not scoped per client. Reusing it with the same body
  returns the existing transfer, including after completion, with
  `Idempotency-Replayed: true`. Reusing it with a different body returns
  `409 IDEMPOTENCY_KEY_REUSED`.
- The fingerprint covers `quote_id`, `client_id` and `recipient` (canonical
  JSON, SHA-256). Header values other than the key are not part of it.
- Concurrent requests with the same key are serialized by the unique index
  and the insert-first flow; exactly one transfer is created.
- The provider-side idempotency key is `{transfer_id}-{provider_id}`. It is
  stable across retries, so a worker crash after sending the request does not
  create a second payout.

### Provider selection

- Eligibility is evaluated against the static `providers.json`: enabled,
  direction support, maintenance window, amount limits in the provider’s
  currency, and a convertible fee.
- Margin is `(client_fee_in_source - provider_fee_in_source) / source_amount
  * 100`. Both fees are converted to the source currency first; the client
  fee may itself be charged in the target currency.
- **Strict minimum margin.** A provider below the direction’s
  `min_margin_percent` is never used, including as a backup. If nothing
  qualifies, the transfer fails with `NO_ELIGIBLE_PROVIDER` and the hold is
  released. A below-minimum fallback would hide a pricing error and lose
  money on every transfer, so an explicit failure is preferred.
- The scoring weights are 50% success rate, 30% margin and 20% latency.
  Margin is capped at 5% and latency at 5 s. Ties are broken by
  `provider_id`. The rationale is in `DECISIONS.md`.
- Provider `success_rate_24h` and `p95_latency_ms` are configuration values,
  not live rolling statistics. A degraded provider keeps its configured
  score until the configuration changes or the process restarts; this is
  listed in `LIMITS.md`.
- Maintenance windows are half-open `[from, to)` and compared against the
  current UTC time. An unparsable window is ignored rather than taking the
  provider down.

### Provider outcomes and fallback

- Only an explicit `422` / `DECLINED` means “no payout was created”; only
  then does the worker try the next provider. If every eligible provider
  declines, the result is `FAILED` with `PROVIDER_DECLINED`.
- A 5xx, timeout, transport error or failed status lookup is **ambiguous**.
  The worker never tries another provider, because the first provider may
  already have created the payout.
- On an ambiguous outcome the worker looks the payout up by `reference`.
  Found → apply `PROCESSING` / `COMPLETED` / `FAILED`. Not found, lookup
  failed, or lookup unavailable → `NEEDS_REVIEW` with the funds held.
- A 404 immediately after a 5xx is **not** treated as proof that no payout
  exists. The mock provider can create an invisible payout for up to 15 s;
  the 30 s review grace is deliberately longer than that visibility lag.
- After the grace, an explicit 404 is accepted as definitive absence:
  `FAILED` `PROVIDER_ERROR`, hold released. A lookup failure keeps the
  transfer in `NEEDS_REVIEW`.
- If the provider later reports a live payout, `NEEDS_REVIEW` moves back to
  `PROCESSING`; the review reason is cleared.

### Webhooks

- The signature is HMAC-SHA256 over the raw body, hex-encoded in
  `X-Signature`, using one deployment-wide `PROVIDER_WEBHOOK_SECRET`.
- `event_id` is the dedupe key. Duplicates, unknown transfers, foreign
  providers and mismatched payout ids are acknowledged with `200` and
  ignored, so the provider does not retry forever.
- Only events strictly newer than `last_event_at` are applied. An event
  without `occurred_at` is applied but does not update the ordering marker.
- Terminal states are sticky; a `COMPLETED` or `FAILED` transfer ignores all
  later events.
- `COMPLETED` debits the reserved amount; `FAILED` releases it; `PROCESSING`
  moves the transfer to `PROCESSING` and clears the review reason.
- Bad signatures return `401`; malformed events return `400`; accepted,
  ignored and duplicate events return `200`.

### Worker and crash recovery

- The worker is one `asyncio` task in the API process. It can be disabled
  with `TRANSFER_WORKER_ENABLED=0`.
- Pending transfers are claimed with a database lease (`claimed_at`,
  default 120 s) and `FOR UPDATE SKIP LOCKED`. A crashed worker’s transfers
  are reclaimed after the lease expires.
- A transfer stays `PENDING` until a provider accepts it. This makes the
  retry path identical to the first attempt: the provider idempotency key is
  stable, so the retry either creates the payout once or returns the
  existing one.
- Reconciliation runs every 2 s (configurable) for `PROCESSING` and
  `NEEDS_REVIEW` transfers. It resolves lost webhooks and the ambiguous
  outcomes described above.
- Settlement locks the transfer row and re-reads it with
  `populate_existing=True`, so a stale ORM copy cannot overwrite a webhook
  that arrived during a slow provider call.

### Daily limit

- The window is rolling 24 hours from `created_at`, not a calendar day.
- Counted states: `PENDING`, `PROCESSING`, `COMPLETED`, `NEEDS_REVIEW`.
  `FAILED` is excluded. Counting `PENDING` prevents queued bursts from
  bypassing the limit; counting `NEEDS_REVIEW` is conservative because the
  funds are still held.
- Each transfer’s source amount is converted to the limit currency at the
  current rate. A missing rate makes the request fail with
  `RATE_UNAVAILABLE` (`400`); the transfer is rolled back.
- The limit check is `spent + requested > limit` (inclusive limit). It runs
  after the balance row lock and in the same transaction as the hold; a
  failure rolls back both the transfer and the hold.
- Same-client, same-currency requests serialize on the balance row. The
  limit is not a separate lockable row, so different currencies can
  overshoot under concurrency; see `LIMITS.md`.

### Configuration

- `DATABASE_URL` takes precedence over `settings.toml`. The full order is
  constructor arguments, `DATABASE_URL`, nested environment variables
  (`DATABASE__URL`), `settings.toml`, field defaults.
- `settings.toml` remains a convenience default for local runs. It is
  intentionally excluded from the Docker image (`.dockerignore`), so the
  container relies on the environment.
- `RATES_URL=snapshot` forces the bundled rate snapshot; this is used by the
  test suite to avoid depending on the running mock provider.

## Remaining deferrals

- The live rate feed is loaded once at application startup; there is no
  periodic refresh. A long-running process will eventually return
  `RATE_UNAVAILABLE` for stale rates instead of refreshing them.
- Level 4 items (structured metrics/tracing, provider hot reload, a
  “why this provider” endpoint, a load-test scenario) are not implemented.
  Migrations are implemented.
- There is no manual-review/admin endpoint for `NEEDS_REVIEW`; the
  reconciliation loop is the only resolver. This and the other gaps are
  listed in `LIMITS.md`.
