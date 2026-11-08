Округление вверх
Операции со временем
дата-классы
alembic
settings.toml
config.py

# Design decisions

The six lines above are the original working notes, kept as-is. The sections
below are the consolidated decision log for the implementation.

## Money, time and persistence

- **Money is `Decimal` end-to-end.** No float is used for calculation or
  persistence. Rounding is `ROUND_HALF_UP` in one place (`Money`); minor units
  come from `data/currencies.json`. Details are in `ASSUMPTIONS.md`.
- **All timestamps are UTC** (`datetime.now(timezone.utc)`), stored as
  `timestamptz`. JSON timestamps use the `...Z` suffix.
- **PostgreSQL + SQLAlchemy async + Alembic.** The schema is migrated on API
  startup; the test profile migrates its own disposable database.
- **`settings.toml` is a local default, not an override.** `DATABASE_URL`
  wins over it (see “Configuration precedence”).

## Architecture: asynchronous API

`POST /transfers` returns `202 Accepted` with a `PENDING` transfer. A
background worker inside the API process dispatches it to a provider.

Why:

- A provider call can take ~30 s in the `slow` scenario. Holding the HTTP
  request open for that long is fragile and gives the client no useful
  progress information.
- The same worker path also retries transfers after a crash, so there is one
  execution path instead of a synchronous path plus a recovery path.
- The transfer and the funds hold are committed before the `202` is returned,
  so the client never loses a request that was acknowledged.
- The client polls `GET /transfers/{id}`. The response contains the current
  status, provider, attempts and the full transition history.

Trade-off: `202` does not mean a provider accepted the transfer. It means the
request is durable and queued. This is documented in the API response and in
`LIMITS.md`.

## State machine

```text
                    client: POST /transfers
                              │
                              ▼
                        ┌───────────┐
                        │  PENDING  │
                        └───────────┘
                          │   │   │
     worker: provider     │   │   └──── worker: ambiguous outcome
     accepted             │   │            (5xx / timeout / transport)
                          │   │                    │
                          │   │                    ▼
                          │   │             ┌──────────────┐
                          │   │             │ NEEDS_REVIEW │
                          │   │             └──────────────┘
                          │   │               │    │    │
                          │   │  provider /   │    │    │  provider /
                          │   │  reconciliation│    │    │  reconciliation
                          │   ▼               ▼    │    ▼
                          │ ┌────────────┐  ┌────────────┐
                          │ │ PROCESSING │  │ PROCESSING │
                          │ └────────────┘  └────────────┘
                          │       │
                          │       │ provider / reconciliation: COMPLETED
                          │       ▼
                          │ ┌───────────┐
                          │ │ COMPLETED │  terminal
                          │ └───────────┘
                          │
                          │ worker: no eligible provider
                          │ or every provider declined
                          ▼
                    ┌────────┐
                    │ FAILED │  terminal
                    └────────┘
```

`NEEDS_REVIEW` can also go directly to `COMPLETED` or `FAILED`, and
`PROCESSING` can go to `FAILED`; those arrows are omitted above to keep the
diagram readable.

| From | To | Initiator | Trigger | Funds |
|---|---|---|---|---|
| — | `PENDING` | client | `POST /transfers` | `available → reserved` |
| `PENDING` | `PROCESSING` | background worker | provider returned `201` / `PROCESSING` | hold unchanged |
| `PENDING` | `COMPLETED` | background worker | provider returned `COMPLETED` synchronously | `reserved → debited` |
| `PENDING` | `FAILED` | background worker | no eligible provider, or all providers declined | `reserved → available` |
| `PENDING` | `NEEDS_REVIEW` | background worker | 5xx, timeout, transport error or failed lookup | hold unchanged |
| `PENDING` | `PROCESSING` / `COMPLETED` / `FAILED` | provider | webhook arrived before the worker recorded the result | according to status |
| `PROCESSING` | `COMPLETED` | provider / reconciliation | completed webhook or provider lookup | `reserved → debited` |
| `PROCESSING` | `FAILED` | provider / reconciliation | failed webhook or provider lookup | `reserved → available` |
| `NEEDS_REVIEW` | `PROCESSING` | reconciliation / provider | the payout is now visible and live | hold unchanged |
| `NEEDS_REVIEW` | `COMPLETED` | provider / reconciliation | provider reports completion | `reserved → debited` |
| `NEEDS_REVIEW` | `FAILED` | reconciliation / provider | provider reports failure, or explicit 404 after the review grace | `reserved → available` |
| `COMPLETED` / `FAILED` | — | — | terminal; later events are ignored | unchanged |

Notes:

- `PENDING` means “queued, not yet accepted by a provider”. It is not a
  failure state and does not imply that the provider was called.
- `PROCESSING` means at least one provider accepted a payout; `provider_id`
  and `provider_payout_id` are recorded.
- `NEEDS_REVIEW` means “the provider outcome is unknown”. Funds stay
  reserved. It is explicitly non-terminal and is resolved by reconciliation
  or a webhook.
- `COMPLETED` and `FAILED` are terminal and sticky. A contradictory or
  duplicate event can never move a transfer out of a terminal state, and can
  never debit or release funds twice.

## Funds invariant

For every client and currency:

```text
available + reserved + debited = starting balance
```

`starting balance` is the value seeded from `data/clients.json`. `debited` is
not stored as a separate column; it is derived as
`starting - available - reserved`. The API exposes `available` and
`reserved`; the invariant is checked by the tests and by
`scripts/chaos_run.py`.

All balance changes happen in one transaction with a row lock on
`balances(client_id, currency)`:

- **Hold:** `available -= amount`, `reserved += amount`.
- **Release:** `reserved -= amount`, `available += amount`.
- **Debit:** `reserved -= amount` (the amount becomes “debited”).

The hold is created in the same transaction as the transfer insert. A
rejected transfer (insufficient funds, daily limit, validation) rolls back
both the insert and the hold. A transfer that fails later releases the hold;
a transfer that completes debits it. Settlement functions first lock the
transfer row and check that it is not terminal, so a duplicate webhook or a
late worker response cannot apply a second balance change.

## Idempotency

- `Idempotency-Key` is mandatory. A missing or blank header returns
  `VALIDATION_ERROR` (`422`).
- **Same key + same request body** returns the existing transfer with
  `202 Accepted` and `Idempotency-Replayed: true`. The body is compared by a
  SHA-256 fingerprint of the canonical JSON of `quote_id`, `client_id` and
  `recipient`. The response always reflects the current status, so a replay
  after completion returns `COMPLETED`, not the original `PENDING`.
- **Same key + different body** returns `409 IDEMPOTENCY_KEY_REUSED`. The
  existing transfer is not modified.
- **Concurrency:** the unique index on `transfers.idempotency_key` is the
  authority. The request inserts first with `ON CONFLICT DO NOTHING`; a
  concurrent duplicate blocks until the first transaction commits and then
  reads the winner. This works before, during and after execution.
- The provider call has its own idempotency key,
  `{transfer_id}-{provider_id}`. It is stable across worker retries, so a
  retry after a crash or lease expiry replays the provider’s existing payout
  instead of creating a second one.

## Provider selection

A provider is eligible only when all of the following hold:

1. `enabled` is true;
2. the transfer’s `direction_id` is in the provider’s `directions`;
3. the current time is outside every `maintenance_windows` interval
   (half-open `[from, to)`);
4. the payout amount converted to the provider’s currency is within the
   provider’s `limits.min`…`limits.max` (inclusive);
5. the provider fee and the client fee can both be converted to the source
   currency;
6. the provider’s margin is **at least** the direction’s
   `min_margin_percent`.

Margin is computed in source currency:

```text
client_fee_source   = client fee converted to source currency
provider_fee_source = provider fixed + percent * target_in_provider_currency,
                      converted to source currency
margin_amount       = client_fee_source - provider_fee_source
margin_percent      = margin_amount / source_amount * 100
```

**Strict minimum margin.** A provider below `min_margin_percent` is never
used, not even as a fallback. If no provider qualifies, the transfer fails
with `NO_ELIGIBLE_PROVIDER` and the hold is released. Silently using a
below-minimum provider would make every such transfer lose money and would
hide a pricing/configuration error; an explicit failure is safer and easier
to operate. This is a deliberate answer to the assignment’s question about a
backup provider with a margin below the minimum.

Eligible providers are ranked by:

```text
score = 0.5 * success_rate_24h
      + 0.3 * margin_score
      + 0.2 * latency_score

margin_score  = clamp(margin_percent / 5%, 0, 1)
latency_score = max(0, 1 - p95_latency_ms / 5000)
```

Rationale:

- **Reliability (50%)** dominates: a failed payout costs support time,
  possibly a double payout, and client trust.
- **Margin (30%)** prefers better economics, but the cap at 5% prevents a
  single high-margin provider from winning forever.
- **Latency (20%)** improves the client experience; the 5 s ceiling means
  providers slower than 5 s get no latency credit.
- Ties are broken by `provider_id` (ascending) so the choice is
  deterministic and reproducible.

The provider’s `success_rate_24h` and `p95_latency_ms` are read from
`providers.json`. They are static configuration, not live rolling metrics;
see `LIMITS.md`.

## Provider outcome policy

The worker distinguishes an **explicit decline** from an **ambiguous
outcome**:

- **`422` / `DECLINED`** means the provider explicitly refused the payout
  and did not create one. This is safe to retry with the next provider in
  the ranked list. If every eligible provider declines, the transfer fails
  with `PROVIDER_DECLINED`; the attempts list contains every provider tried.
- **5xx, timeout, transport error or a failed status lookup** is ambiguous:
  the provider may have created the payout even though the caller did not
  receive a success response. The worker **never** tries another provider in
  this case, because that could pay the recipient twice.
- On an ambiguous outcome the worker immediately looks the payout up by
  `reference`:
  - found and `COMPLETED` / `FAILED` → apply that status;
  - found and `PROCESSING` → move to `PROCESSING`;
  - not found, lookup failed, or lookup unavailable → `NEEDS_REVIEW`, funds
    stay reserved.
- **A 404 immediately after a 5xx is not proof of absence.** The mock
  provider’s chaos mode can create a payout that stays invisible for up to
  15 s. The worker therefore treats the first 404 as “unknown”, not as “no
  payout”.
- **Review grace:** a transfer in `NEEDS_REVIEW` is left untouched for
  `TRANSFER_REVIEW_GRACE_SEC` (default 30 s) after entering review.
  Reconciliation does not draw conclusions during this window. After the
  grace:
  - provider reports `COMPLETED` / `FAILED` → apply it;
  - provider reports `PROCESSING` → move back to `PROCESSING`;
  - explicit 404 → the provider never created a payout → `FAILED`
    `PROVIDER_ERROR`, release the hold;
  - lookup still failing → stay in `NEEDS_REVIEW`, retry later.
- A transfer that stays `NEEDS_REVIEW` forever is an operational signal,
  not a silent loss: the client’s money is still reserved and visible in the
  balance endpoint.

## Webhooks

`POST /webhooks/{provider_id}` accepts provider callbacks.

- **Signature:** HMAC-SHA256 over the raw request body with
  `PROVIDER_WEBHOOK_SECRET`, sent as hex in `X-Signature`. The comparison is
  constant-time. A missing or invalid signature returns `401
  INVALID_SIGNATURE`; the body is not parsed before verification.
- **Dedupe:** `webhook_events.event_id` is the primary key. A duplicate
  event is acknowledged with `{"status": "duplicate"}` and has no effect.
  Dedupe happens before the transfer lookup, so a duplicate cannot debit or
  release twice.
- **Unknown or foreign events:** an event for an unknown transfer, a
  different provider, or a different payout id is acknowledged (`200`) and
  ignored. Acknowledging prevents the provider from retrying forever; the
  event is stored for audit.
- **Ordering:** only events with a strictly newer `occurred_at` than
  `transfers.last_event_at` are applied. An older event is ignored. An event
  without `occurred_at` is applied but does not update the ordering marker;
  see `LIMITS.md` for the consequence.
- **Terminal states are sticky:** a `COMPLETED` or `FAILED` transfer ignores
  every later event, including a contradictory one. This is what prevents a
  late “processing” or “failed” webhook from reversing a completed payout.
- **Effects:** `PROCESSING` moves the transfer to `PROCESSING` and clears any
  review reason; `COMPLETED` debits the hold; `FAILED` releases the hold.
- **Responses:** `200` for accepted, ignored and duplicate events; `400` for
  malformed JSON or a missing/invalid `event_id` / `reference` / `status`;
  `401` for a bad signature.

## Worker lease and crash recovery

- The worker is one `asyncio` task started in the FastAPI lifespan. It can
  be disabled with `TRANSFER_WORKER_ENABLED=0` (tests do this when they drive
  the worker manually).
- `claim_pending` selects `PENDING` rows whose `claimed_at` is `NULL` or
  older than `TRANSFER_CLAIM_TIMEOUT_SEC` (default 120 s), uses
  `FOR UPDATE SKIP LOCKED`, sets `claimed_at`, and commits. A crashed worker
  therefore loses only its lease; the transfer is reclaimed after the
  timeout. Two workers cannot claim the same row at the same time.
- The transfer stays `PENDING` until a provider accepts. If the process dies
  between “sent to provider” and “recorded the result”, the retry uses the
  same `{transfer_id}-{provider_id}` key, so the provider returns the
  existing payout instead of creating a new one.
- Reconciliation runs every `TRANSFER_RECONCILE_SEC` (default 2 s) for
  `PROCESSING` and `NEEDS_REVIEW` transfers older than
  `TRANSFER_RECONCILE_MIN_AGE_SEC` (default 1 s). It handles lost webhooks
  and ambiguous outcomes.
- Settlement reads the transfer with `SELECT ... FOR UPDATE` plus
  `populate_existing=True`. The second part matters: without it, a session
  that loaded the transfer before a slow provider call could keep a stale
  identity-map copy and overwrite a webhook that arrived in the meantime.
  This was a real bug found by the chaos run (see “AI tools used”).

## Daily limit

- The limit is a **rolling 24-hour window** from `created_at`, not a
  calendar day. The cutoff is `now - 24h`.
- Counted states: `PENDING`, `PROCESSING`, `COMPLETED`, `NEEDS_REVIEW`.
  `FAILED` is excluded. Counting `PENDING` prevents a burst of queued
  transfers from bypassing the limit; counting `NEEDS_REVIEW` is
  conservative because those funds are still held.
- Each transfer’s `source_amount` is converted to the limit currency with
  the current rate service. The limit check is inclusive: a transfer is
  rejected when `spent + requested > limit`.
- The check runs **after** the balance row lock in the same transaction as
  the hold. If it fails, the transfer insert and the hold are rolled back.
  For the same client and currency, concurrent submissions serialize on the
  balance row, so the 24-hour sum cannot be double-spent.
- The check is not global across currencies; see `LIMITS.md`.

## Configuration precedence

`DATABASE_URL` is the documented deployment variable, but the original
settings code only understood the nested `DATABASE__URL`. The bundled
`settings.toml` therefore shadowed `DATABASE_URL`, and local tests silently
ran against the main database, where the running API worker raced them.

The source order is now explicit:

1. constructor arguments;
2. `DATABASE_URL` (`EnvDatabaseUrlSource`);
3. nested environment variables such as `DATABASE__URL`;
4. `settings.toml`;
5. field defaults.

`tests/test_config.py` contains regression tests for both directions:
`DATABASE_URL` overrides the TOML file, and the TOML file is still used when
no environment variable is set.

## AI tools used

The implementation was built with the **OpenCode CLI coding agent running
DeepSeek V4.1 Flash Free**. It was used for:

- reviewing existing Level 1 quote engine, tests, API, Money processing and Level 2 skeleton: existing transfer routes and SQLAlchemy tables/repositories.
- scaffolding the remaining FastAPI routes, completing SQLAlchemy tables/repositories and the
  background worker;
- writing the pytest suite (unit, API and integration tests);
- running the chaos harness and analysing its report;
- drafting this documentation.

The human author set the requirements, reviewed every change, chose the
policies (async API, strict margin, ambiguity handling, review grace) and
verified the behaviour manually against the Docker stack.

## Where the AI erred and how it was caught

1. **Settings precedence (silent test/main-DB collision).** The AI wrote the
   settings source order and the README independently: the README promised
   `DATABASE_URL`, but the settings class only read the nested
   `DATABASE__URL`, so `settings.toml` won. The test suite appeared flaky:
   the API container’s worker was claiming test transfers in the main
   database. The AI first chased the symptom (worker claiming, stale leases)
   and added temporary debug instrumentation before finding the real cause.
   Fixed with `EnvDatabaseUrlSource`, an explicit source order and
   `tests/test_config.py`; the temporary debug code was removed.

2. **Stale ORM identity-map settlement.** The worker loaded a transfer,
   called the provider (up to 35 s), and then settled using the session’s
   cached object. A webhook could complete the transfer in the meantime, and
   the late worker write would overwrite the terminal state and debit/release
   again. Chaos produced double payouts and “failed but paid” transfers. Fixed
   by re-reading with `SELECT ... FOR UPDATE` and
   `populate_existing=True`, plus the regression test
   `test_late_dispatch_cannot_overwrite_a_completed_webhook`.

3. **Unsafe 5xx fallback.** The first implementation treated a 404 lookup
   after a 5xx as proof that the provider had no payout and moved to the next
   provider. The mock provider’s `ERROR_CREATED` / `DROP_CREATED` chaos
   actions create a payout that can be invisible for up to 15 s, so the 404
   was not proof; chaos found three double payouts. Fixed by treating every
   non-decline failure as ambiguous, never falling back, and adding the
   30 s review grace before a 404 is accepted as definitive.

4. **Flaky-test diagnosis.** Before finding the configuration bug, the AI
   spent time instrumenting `claim_pending` and the worker instead of
   checking which database the tests actually used. The lesson recorded here
   is to verify the environment boundary (which DB, which process) before
   instrumenting application logic.

5. **Temporary diagnostics.** The AI added `worker debug:` logging and stack
   traces while investigating. They were removed before the final run; the
   final code contains no debug-only branches.
