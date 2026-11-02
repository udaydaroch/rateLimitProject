# Ledgerline: a rate-limited banking API platform

A Bank-as-a-Service public API. Fintech developers sign up, get API keys, and
call endpoints for accounts, transfers and payouts. The platform enforces
per-key rate limits, daily quotas and fraud controls, and meters usage for
billing.

All four classic rate limiting algorithms are implemented, and each one guards
the part of the bank it suits best.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload
```

- Dashboard: http://127.0.0.1:8000/dashboard (sign up there, then use the **Rate limit lab** buttons to watch each algorithm work)
- Interactive API docs: http://127.0.0.1:8000/docs
- Tests: `.venv/bin/pytest -q`

## Which algorithm protects what, and why

| Banking concern | Algorithm | Keyed by | Why this algorithm |
|---|---|---|---|
| General API traffic (balances, history) | **Token bucket** (free: burst 10, 2/s) | API key | Banking apps burst on launch and then go quiet. Token bucket allows the burst and still caps the average. |
| Daily quota and billing | **Fixed window** (free: 1,000/day) | Customer | A quota is a contract term. Customers need a predictable reset time ("00:00 UTC") that can go on an invoice. |
| Transfer velocity (anti-fraud) | **Sliding window log** (5 per rolling 60s) | Source account | Must be exact. A fixed window lets an attacker send 5 transfers at 12:00:59 and 5 more at 12:01:00. A sliding window has no boundary to exploit. |
| Payouts to the payment rail | **Leaky bucket** (queue 20, drains 1/s) | Customer | The clearing network wants a steady flow, not spikes. A payroll batch (`/v1/payouts/batch`) is accepted in one call and dispatched smoothly. Each payout returns its `dispatch_at`, and overflow is rejected per item. |
| Brute-forcing API keys | **Fixed window** (20 failures/min) | Client IP | Cheap, one counter per IP. Boundary bursts don't matter here. |

`tests/test_strategies.py::test_fixed_window_allows_boundary_burst` and
`test_sliding_window_blocks_boundary_burst` demonstrate the transfer-velocity
argument directly.

## Design: Strategy pattern

```
app/ratelimit/
  base.py            RateLimitStrategy (interface) + Decision
  token_bucket.py    ┐
  sliding_window.py  │ concrete strategies, one algorithm each
  fixed_window.py    │ (each implements _evaluate / reset / describe)
  leaky_bucket.py    ┘
  limiter.py         RateLimiter: the context the app talks to
  factory.py         create_strategy("sliding_window", limit=5, window=60)
```

- **`RateLimitStrategy`** defines the contract. `acquire()` and `peek()` are
  implemented once in the base class: they take the lock and call the abstract
  `_evaluate(key, cost, consume)`. Every algorithm gets non-consuming `peek()`
  without duplicating it.
- **`RateLimiter`** (context) wraps a strategy, namespaces keys, and exposes
  `set_strategy()` to swap algorithms at runtime. Endpoints and middleware only
  depend on `RateLimiter`, never on a concrete algorithm.
- **`create_strategy`** (factory) maps config names to classes, so
  [app/policies.py](app/policies.py) is plain data. To move auth lockout to a
  sliding window, change one tuple.

## Other design points

- **Hashed API keys**: format `bk_live_<prefix>_<secret>`. Only
  `HMAC-SHA256(pepper, secret)` is stored. The prefix is an indexed lookup id,
  and comparison is constant-time. The full key is shown once at creation.
  ([app/security.py](app/security.py))
- **Idempotency keys**: `POST /v1/transfers` and `/v1/payouts` require an
  `Idempotency-Key`. Retries replay the stored response
  (`Idempotent-Replayed: true`) and don't count against velocity limits. A
  reused key with a different body gets 422; a concurrent duplicate gets 409.
  429 and 5xx responses aren't stored, so a client can retry with the same key.
  The money movement and the stored response commit in one transaction.
  ([app/idempotency.py](app/idempotency.py))
- **Usage metering**: an ASGI middleware records one event per authenticated
  call, 429s included, with the final status code. `GET /v1/usage` reports daily
  usage, per-endpoint usage, quota, and a billing estimate. Pro plans keep
  working past quota and are billed overage. ([app/middleware.py](app/middleware.py), [app/metering.py](app/metering.py))
- **Rate limit headers**: `RateLimit-Policy`, `RateLimit-Limit`,
  `RateLimit-Remaining`, `RateLimit-Reset`, plus `Retry-After` on 429 and
  `X-Daily-Quota-Remaining`.
- **Cursor pagination**: transactions and payouts use opaque keyset cursors
  (`id < last_seen`). Offset pagination would skip or duplicate rows on a ledger
  that's being written to. ([app/pagination.py](app/pagination.py))
- **Money**: integer cents, a `CHECK (balance_cents >= 0)` constraint, and
  `BEGIN IMMEDIATE` transactions.

## API

| Method | Path | Limits applied |
|---|---|---|
| POST | `/v1/signup` | – (returns first API key) |
| GET/POST/DELETE | `/v1/keys`, `/v1/keys/{id}` | management: not limited or billed |
| GET | `/v1/accounts`, `/v1/accounts/{id}/balance` | token bucket + quota |
| GET | `/v1/accounts/{id}/transactions?limit=&cursor=` | token bucket + quota |
| POST | `/v1/transfers` (Idempotency-Key) | + sliding window per account |
| POST | `/v1/payouts` (Idempotency-Key) | + leaky bucket per customer |
| POST | `/v1/payouts/batch` (Idempotency-Key) | 1 token for the call, leaky bucket per payout |
| GET | `/v1/payouts?cursor=` | token bucket + quota |
| GET | `/v1/usage` | management: not limited or billed |

Authenticate with `X-API-Key: bk_live_…` or `Authorization: Bearer bk_live_…`.

Management endpoints (usage, keys) still require a valid key but don't consume
rate limits or quota. A customer whose API bucket is empty can still open the
dashboard to find out why.

## Production notes

Limiter state is in-process memory, which works for a single instance. With
several instances behind a load balancer, move each strategy's state to Redis
(for example, a Lua script per algorithm for atomic check-and-update). The
Strategy interface doesn't change; only the storage inside each strategy does.
