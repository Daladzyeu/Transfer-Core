# Limits and known gaps

This file records what is not implemented, what will break under conditions
outside the assignment’s scenarios, and which assumptions are load-bearing.

## Status

- Level 1 (quote calculation) is implemented and tested.
- Level 2 (execution, provider selection, state machine, idempotency,
  reserve/release) is implemented and tested.
- Level 3 (reliability) is implemented for the questions below: ambiguous
  provider outcomes, idempotency before/during/after execution, concurrent
  balance use, webhook duplicates/ordering/unknown payouts, crash recovery,
  reconciliation, and the daily limit. The remaining caveats are in this
  file.
- Level 4 is not implemented except for Alembic migrations.

## Not implemented

- **No transfer list or search endpoint.** A client must keep the
  `transfer_id` from the creation response (or replay the idempotency key).
- **No manual-review/admin endpoint.** `NEEDS_REVIEW` is resolved only by the
  reconciliation loop or a provider webhook. There is no “approve/reject”
  API and no UI.
- **No authentication or authorization.** `client_id` is trusted from the
  request body. There is no API key, OAuth, mTLS, or per-client isolation.
  Do not expose this service to untrusted networks as-is.
- **No per-provider webhook secret.** One deployment-wide
  `PROVIDER_WEBHOOK_SECRET` signs every provider’s callbacks.
- **No webhook timestamp/skew validation.** The signature is checked, and
  `occurred_at` is used for ordering, but an event with a valid signature
  and an arbitrary future timestamp is accepted. A leaked secret can forge
  events.
- **No rate-feed refresh.** Rates are loaded once at startup. After the
  freshness window (6 hours relative to the feed), quotes fail with
  `RATE_UNAVAILABLE`; the process does not fetch new rates on its own.
- **No structured metrics or tracing.** Logs are plain text; there are no
  Prometheus metrics, OpenTelemetry spans, or per-transfer correlation ids
  beyond the `transfer_id` in log messages.
- **No provider hot reload.** `providers.json` is read at startup. Changing
  a provider’s status, limits, fees, or maintenance window requires a
  restart.
- **No “why this provider” endpoint.** The scoring inputs and the ranked
  list are not exposed through the API; only the chosen `provider_id` and
  the attempts are returned. `ProviderRouter.evaluate` exists for tests and
  future use.
- **No load-test scenario beyond `scripts/chaos_run.py`.** The chaos script
  exercises correctness under concurrency, not sustained throughput.
- **No dead-letter queue or alerting.** A transfer that stays in
  `NEEDS_REVIEW` because the provider never answers is visible in the API
  and the database, but nothing pages an operator.
- **No automatic retry of a `FAILED` transfer.** The client must create a
  new quote and a new transfer. A `FAILED` transfer is terminal by design.
- **No FX hedging or rate lock beyond the quote.** The quote price is fixed
  at creation; the provider payout uses the target amount from the quote.
- **No idempotency-key TTL or cleanup.** `transfers` grows forever; old
  keys are never reused automatically.
- **No pagination of `attempts` / `transitions`.** They are JSON arrays on
  the transfer row. A transfer with many retries or events grows the row and
  the response.
- **No encryption of recipient PII at rest.** Names and account numbers are
  stored as JSONB in PostgreSQL.
- **No CSRF/rate limiting.** The API is a backend service and assumes a
  trusted caller.

## What will break, and why

### Review grace vs provider visibility lag

The 30 s `TRANSFER_REVIEW_GRACE_SEC` is chosen to be longer than the mock
provider’s maximum 15 s visibility lag. If a real provider can hide a created
payout for longer than the grace, reconciliation can see a 404 after the
grace, mark the transfer `FAILED`, and release the client’s funds — while the
provider later completes the payout. That is a double-spend: the recipient is
paid and the client is refunded.

Mitigation: increase `TRANSFER_REVIEW_GRACE_SEC` to a value above the
provider’s documented visibility lag, or make the grace provider-specific.
There is no adaptive grace in this implementation.

### Eventual consistency in general

A 404 is treated as definitive only after the grace. Any provider whose read
model can lag longer than the configured grace, or can return 404 for an
existing payout intermittently, breaks that assumption.

### Daily limit across different currencies

The limit check is serialized by the balance row lock for the **same client
and same currency**. Two concurrent transfers in different currencies lock
different balance rows and can both read the same 24-hour total, so the
client can exceed the limit by roughly the sum of the in-flight amounts. The
same is true for a client with multiple balance currencies.

The 24-hour total is also converted at the **current** rate. A rate change
changes the computed historical spend; the limit is therefore approximate
for transfers originally made in another currency. A production system would
store the limit-currency value at creation time and use a per-client limit
row (or advisory lock) for serialization.

### Daily limit counts only `source_amount`

The fee is part of the gross source amount debited from the client, so the
limit counts the full debit. If the business definition of the limit were
“amount to convert only” or “fee excluded”, the check would need to change.

### Static provider statistics

`success_rate_24h` and `p95_latency_ms` come from `providers.json`. They are
not rolling live metrics. A provider that degrades after startup keeps its
score until restart; a provider that improves is underrated. A real system
would compute these from settlement history and refresh them periodically.

### Static provider fees and limits

The margin check uses the provider fee configuration, not the provider’s
actual charged fee. If a provider changes its fee without a config update,
the realized margin can be below `min_margin_percent` even though the router
accepted the provider.

### Webhook ordering without `occurred_at`

If a provider omits `occurred_at`, the event is applied but `last_event_at`
is not updated. A later event with an older timestamp can then be applied
after it. Providers that always send `occurred_at` are unaffected. Events
with equal timestamps are ignored (strictly newer is required), so two
events with the same timestamp are resolved by arrival order only for the
first one.

### Webhook dedupe by `event_id` only

If a provider reuses an `event_id` for a different transfer or status, the
second event is treated as a duplicate and ignored. The provider is expected
to use globally unique event ids.

### Single process, single worker task

The worker runs inside the API process. Multiple API replicas each run a
worker; the database lease and `SKIP LOCKED` keep claims safe, and
settlement locks the transfer row, so this is safe for correctness. It is
not a distributed scheduler: there is no leader election, no global
concurrency limit across replicas, and no backpressure. Each replica uses
its own `TRANSFER_WORKER_CONCURRENCY` (default 5) and batch size (default 10).

### Long provider calls hold a database connection

`dispatch` keeps an `AsyncSession` open while calling the provider (up to the
35 s create timeout). With `NullPool`, each in-flight dispatch uses a
connection for the whole call. Under high concurrency this can exhaust the
PostgreSQL connection limit. A production design would release the session
before the network call, or use a smaller, separate worker pool.

### Reconciliation throughput

Reconciliation processes at most `TRANSFER_WORKER_BATCH` (default 10)
transfers every `TRANSFER_RECONCILE_SEC` (default 2 s), so roughly 5
transfers/s per replica. A large backlog of `NEEDS_REVIEW` transfers drains
slowly. There is no priority queue and no exponential backoff; every eligible
transfer is retried on every cycle.

### Provider retry policy

Only an explicit decline triggers a fallback. A 5xx/timeout parks the
transfer in `NEEDS_REVIEW`; there is no second provider attempt even after
the grace resolves to “no payout”. This is deliberate (safety over
availability), but it means a provider that returns 5xx for every request
will park every transfer in review instead of failing over. The client sees
`NEEDS_REVIEW` and the funds stay reserved until reconciliation releases
them after the grace.

### Provider create timeout vs slow scenario

`PROVIDER_TIMEOUT_SEC` defaults to 35 s. The mock `slow` scenario sleeps 30 s,
so it completes within the timeout. A slower provider would time out and
become an ambiguous `NEEDS_REVIEW` transfer; the worker would not wait
longer. The timeout is configurable.

### Claim lease vs provider timeout

The claim lease is 120 s and the provider create timeout is 35 s, so a
healthy dispatch cannot outlive its lease. If the timeout is raised above the
lease, a second worker can reclaim the transfer while the first call is still
running. The stable provider idempotency key makes the duplicate call safe,
but both workers may race to record the result; settlement locking and
terminal-state stickiness keep the final state consistent.

### Database migrations on startup

The API container runs `alembic upgrade head` before starting. Multiple
replicas starting simultaneously can race the migration. The provided
`docker-compose.yml` runs one API container. A production deployment should
run migrations as a separate job.

### `DATABASE_URL` normalization

`normalize_database_url` rewrites `@db:` to `@localhost:` if the hostname
`db` cannot be resolved. This is a convenience for local runs; in a container
where `db` is temporarily unresolvable, it can point the process at the wrong
host and fail. It never silently changes a resolvable URL.

### Stale quote handling

A transfer cannot be created from an expired quote (`410 QUOTE_EXPIRED`), but
there is no check that the quote’s direction/amount still matches current
limits or provider availability at creation time. The quote price is honored
until it expires; provider selection happens later with the current
`providers.json`.

### Amount precision

Amounts are stored as `Numeric(20, 10)`. The currency registry supports 0–3
minor units. A currency with more than 10 decimal places, or an amount above
20 significant digits, would be truncated/rejected by PostgreSQL. This is
outside the assignment’s data.

### Recipient validation

Only non-empty strings are required for `name`, `account` and `bank_code`.
There is no country-specific account validation. An invalid recipient is
discovered only when the provider declines or fails the payout.

### Rate freshness and cross rates

Rates are considered fresh relative to the newest timestamp in the feed, not
wall-clock time (see `ASSUMPTIONS.md`). Cross rates are derived through one
intermediate currency; a missing direct/inverse/cross rate returns
`RATE_UNAVAILABLE` rather than guessing. A long-running process with a stale
feed will reject quotes instead of refreshing.

## Chaos run result

The final chaos run used 120 transfers at concurrency 8 against
`MOCK_MODE=chaos`, seed 1:

```text
submitted          120
final_success       77  (COMPLETED)
final_failure       41  (FAILED)
final_review         2  (NEEDS_REVIEW)
double_payouts       0
stuck_reserves       0
balance_invariant    0
failed_but_paid      0
success_without_payout 0
amount_mismatch      0
unaccounted_payouts  0
idempotency          0
stuck_transfers      0
api_errors           0
RESULT: OK
```

The two `NEEDS_REVIEW` transfers are the explicitly designated state allowed
by the assignment: their outcome was still unknown when the settle timeout
expired, and their funds remained reserved. They are not “stuck” in the
sense of an unaccounted reserve; reconciliation continues to poll them.

## Test coverage

The suite contains 162 tests and runs against PostgreSQL:

```bash
docker compose --profile test run --rm --build test
```

The tests cover quote examples, validation, rate freshness, money rounding,
provider routing/scoring/strict margin, transfer lifecycle, idempotency,
concurrent submissions, funds invariants, daily limits, webhook signatures
and dedupe, out-of-order events, terminal stickiness, reconciliation, lease
claims, config precedence, and the stale-ORM regression.

The suite is intentionally integration-heavy: it uses the real PostgreSQL
schema and the real repository code. It does not test a live provider; the
provider client is faked in lifecycle tests, and the chaos script exercises
the real mock provider end-to-end.

## Operational notes

- The main database and the test database are separate. Local tests must set
  `DATABASE_URL=.../transfer_test`; the fixture truncates business tables.
- `TRANSFER_WORKER_ENABLED=0` disables the in-process worker (useful for
  API-only deployments or tests that drive the worker manually).
- `TRANSFER_REVIEW_GRACE_SEC`, `TRANSFER_RECONCILE_SEC`,
  `TRANSFER_RECONCILE_MIN_AGE_SEC`, `TRANSFER_CLAIM_TIMEOUT_SEC`,
  `TRANSFER_WORKER_CONCURRENCY`, `TRANSFER_WORKER_BATCH`,
  `TRANSFER_WORKER_POLL_SEC` and `PROVIDER_TIMEOUT_SEC` are the tuning
  knobs.
- `PROVIDER_WEBHOOK_SECRET` must match the mock provider’s secret. The
  default is a local-development value, not a production secret.
