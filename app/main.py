"""Bank-as-a-Service public API with per-key rate limits, quotas and usage billing.

Run:  .venv/bin/uvicorn app.main:app --reload
Docs: http://127.0.0.1:8000/docs     Dashboard: http://127.0.0.1:8000/dashboard
"""
import random
import time
from pathlib import Path
from typing import List, Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import policies
from .db import Database, IntegrityError, get_db
from .deps import Client, client_ip, enforce, management_client, metered_client
from .idempotency import run_idempotent
from .metering import usage_report
from .middleware import RateLimitHeadersAndMetering
from .pagination import decode_cursor, encode_cursor
from .security import generate_api_key

STATIC = Path(__file__).parent / "static"
MAX_TRANSFER_CENTS = 1_000_000_00  # $1,000,000


DESCRIPTION = """
A sandbox banking API that demonstrates four rate limiting algorithms.

1. `POST /v1/signup` to get an API key (shown once).
2. Click **Authorize** and paste the key.
3. Call endpoints and watch the `RateLimit-*` response headers.
   `GET /v1/limits` shows the live internal state of every limiter for your key.

Everything here is sandbox money. Data lives in Postgres and limiter state in
Redis, so limits hold across every serverless instance.
The visual dashboard is at [/dashboard](/dashboard), and a step-by-step guide to how
everything works is at [/guide](/guide).
"""

app = FastAPI(title="Ledgerline Bank API", version="1.0.0", description=DESCRIPTION)
app.add_middleware(RateLimitHeadersAndMetering)


# ---------- models ----------

class SignupIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=254)
    plan: Literal["free", "pro"] = "free"


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=50)


class TransferIn(BaseModel):
    from_account_id: int
    to_account_id: int
    amount_cents: int = Field(gt=0, le=MAX_TRANSFER_CENTS)
    description: str = Field(default="Transfer", max_length=140)


class PayoutIn(BaseModel):
    from_account_id: int
    amount_cents: int = Field(gt=0, le=MAX_TRANSFER_CENTS)
    beneficiary: str = Field(min_length=1, max_length=100)


class PayoutItem(BaseModel):
    amount_cents: int = Field(gt=0, le=MAX_TRANSFER_CENTS)
    beneficiary: str = Field(min_length=1, max_length=100)


class PayoutBatchIn(BaseModel):
    from_account_id: int
    payouts: List[PayoutItem] = Field(min_length=1, max_length=100)


# ---------- helpers ----------

def _own_account(db: Database, client: Client, account_id: int) -> dict:
    row = db.execute("SELECT * FROM accounts WHERE id = ? AND customer_id = ?",
                     (account_id, client.customer_id)).fetchone()
    if row is None:
        raise HTTPException(404, {"error": "account_not_found"})
    return row


def _account_json(row) -> dict:
    return {"id": row["id"], "name": row["name"], "currency": row["currency"],
            "balance_cents": row["balance_cents"]}


def _debit(db: Database, account_id: int, amount: int, description: str, now: float) -> None:
    # Check and debit in one atomic statement, so concurrent debits can't overdraw.
    cur = db.execute("UPDATE accounts SET balance_cents = balance_cents - ? WHERE id = ? AND balance_cents >= ?",
                     (amount, account_id, amount))
    if cur.rowcount == 0:
        balance = db.execute("SELECT balance_cents FROM accounts WHERE id = ?", (account_id,)).fetchone()
        raise HTTPException(422, {"error": "insufficient_funds", "balance_cents": balance["balance_cents"]})
    db.execute("INSERT INTO transactions (account_id, amount_cents, description, created_at) VALUES (?, ?, ?, ?)",
               (account_id, -amount, description, now))


def _queue_payout(db: Database, account_id: int, amount: int, beneficiary: str,
                  now: float, delay: float) -> int:
    _debit(db, account_id, amount, f"Payout to {beneficiary}", now)
    return db.execute(
        "INSERT INTO payouts (account_id, amount_cents, beneficiary, status, created_at, dispatch_at) "
        "VALUES (?, ?, ?, 'queued', ?, ?) RETURNING id",
        (account_id, amount, beneficiary, now, now + delay)).fetchone()["id"]


def _seed_demo_accounts(db: Database, customer_id: int, now: float) -> None:
    rng = random.Random(customer_id)
    checking = db.execute("INSERT INTO accounts (customer_id, name, balance_cents) "
                          "VALUES (?, 'Checking', 500000) RETURNING id", (customer_id,)).fetchone()["id"]
    db.execute("INSERT INTO accounts (customer_id, name, balance_cents) VALUES (?, 'Savings', 2000000)",
               (customer_id,))
    merchants = ["Coffee Co", "Grocer", "Metro Transit", "Bookshop", "Salary", "Rent", "Utilities"]
    rows = []
    for i in range(60):
        merchant = rng.choice(merchants)
        amount = rng.randint(30000, 400000) if merchant == "Salary" else -rng.randint(300, 25000)
        rows.append((checking, amount, merchant, now - (60 - i) * 3600))
    db.executemany("INSERT INTO transactions (account_id, amount_cents, description, created_at) VALUES (?, ?, ?, ?)",
                   rows)


# ---------- onboarding & keys ----------

@app.post("/v1/signup", status_code=201, tags=["keys"])
def signup(body: SignupIn, request: Request, db: Database = Depends(get_db)):
    """Create a customer, two demo accounts, and the first API key (shown once)."""
    # Reject duplicates before taking a signup slot, so a double-click doesn't use up
    # the IP's hourly allowance. The UNIQUE constraint still catches races below.
    if db.execute("SELECT 1 FROM customers WHERE email = ?", (body.email,)).fetchone():
        raise HTTPException(409, {"error": "email_taken"})
    enforce(request, policies.limiters.signups, f"ip:{client_ip(request)}",
            "Too many signups from this IP. Try again within the hour.")
    now = time.time()
    db.begin()
    try:
        customer_id = db.execute("INSERT INTO customers (name, email, plan, created_at) VALUES (?, ?, ?, ?) "
                                 "RETURNING id", (body.name, body.email, body.plan, now)).fetchone()["id"]
        full_key, prefix, key_hash = generate_api_key()
        db.execute("INSERT INTO api_keys (customer_id, name, prefix, key_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                   (customer_id, "default", prefix, key_hash, now))
        _seed_demo_accounts(db, customer_id, now)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, {"error": "email_taken"})
    return {"customer_id": customer_id, "plan": body.plan, "api_key": full_key,
            "warning": "Store this key now. Only a hash is kept; it cannot be shown again."}


@app.get("/v1/keys", tags=["keys"])
def list_keys(client: Client = Depends(management_client), db: Database = Depends(get_db)):
    rows = db.execute("SELECT id, name, prefix, created_at, last_used_at, revoked_at FROM api_keys "
                      "WHERE customer_id = ? ORDER BY id", (client.customer_id,)).fetchall()
    return {"data": [{**dict(r), "hint": f"bk_live_{r['prefix']}_…"} for r in rows]}


@app.post("/v1/keys", status_code=201, tags=["keys"])
def create_key(body: KeyIn, client: Client = Depends(management_client), db: Database = Depends(get_db)):
    full_key, prefix, key_hash = generate_api_key()
    key_id = db.execute("INSERT INTO api_keys (customer_id, name, prefix, key_hash, created_at) "
                        "VALUES (?, ?, ?, ?, ?) RETURNING id",
                        (client.customer_id, body.name, prefix, key_hash, time.time())).fetchone()["id"]
    return {"id": key_id, "name": body.name, "api_key": full_key}


@app.delete("/v1/keys/{key_id}", tags=["keys"])
def revoke_key(key_id: int, client: Client = Depends(management_client), db: Database = Depends(get_db)):
    cur = db.execute("UPDATE api_keys SET revoked_at = ? WHERE id = ? AND customer_id = ? AND revoked_at IS NULL",
                     (time.time(), key_id, client.customer_id))
    if cur.rowcount == 0:
        raise HTTPException(404, {"error": "key_not_found"})
    return {"id": key_id, "revoked": True}


# ---------- accounts (token bucket + daily quota) ----------

@app.get("/v1/accounts", tags=["banking"])
def list_accounts(client: Client = Depends(metered_client), db: Database = Depends(get_db)):
    rows = db.execute("SELECT * FROM accounts WHERE customer_id = ? ORDER BY id", (client.customer_id,)).fetchall()
    return {"data": [_account_json(r) for r in rows]}


@app.get("/v1/accounts/{account_id}/balance", tags=["banking"])
def get_balance(account_id: int, client: Client = Depends(metered_client), db: Database = Depends(get_db)):
    return _account_json(_own_account(db, client, account_id))


@app.get("/v1/accounts/{account_id}/transactions", tags=["banking"])
def list_transactions(
    account_id: int,
    limit: int = Query(20, ge=1, le=100),
    cursor: Optional[str] = None,
    client: Client = Depends(metered_client),
    db: Database = Depends(get_db),
):
    _own_account(db, client, account_id)
    before_id = decode_cursor(cursor)
    sql = "SELECT id, amount_cents, description, created_at FROM transactions WHERE account_id = ?"
    args: list = [account_id]
    if before_id is not None:
        sql += " AND id < ?"
        args.append(before_id)
    rows = db.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit + 1)).fetchall()

    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "data": [dict(r) for r in rows],
        "has_more": has_more,
        "next_cursor": encode_cursor(rows[-1]["id"]) if has_more else None,
    }


# ---------- transfers (sliding window velocity + idempotency) ----------

@app.post("/v1/transfers", status_code=201, tags=["banking"])
def create_transfer(
    body: TransferIn,
    request: Request,
    idempotency_key: str = Header("", alias="Idempotency-Key"),
    client: Client = Depends(metered_client),
    db: Database = Depends(get_db),
):
    """Move money between accounts. Requires an Idempotency-Key header.

    A retried request with the same key is answered from the stored response
    and does not count against the transfer velocity limit again.
    """
    def handler() -> dict:
        src = _own_account(db, client, body.from_account_id)
        if body.to_account_id == src["id"]:
            raise HTTPException(422, {"error": "same_account"})
        if db.execute("SELECT 1 FROM accounts WHERE id = ?", (body.to_account_id,)).fetchone() is None:
            raise HTTPException(404, {"error": "destination_not_found"})

        enforce(request, policies.limiters.transfer_velocity, f"acct:{src['id']}",
                "Transfer velocity limit: at most 5 outgoing transfers per account in any 60 seconds.")

        now = time.time()
        _debit(db, src["id"], body.amount_cents, body.description, now)
        db.execute("UPDATE accounts SET balance_cents = balance_cents + ? WHERE id = ?",
                   (body.amount_cents, body.to_account_id))
        tx_id = db.execute("INSERT INTO transactions (account_id, amount_cents, description, created_at) "
                           "VALUES (?, ?, ?, ?) RETURNING id",
                           (body.to_account_id, body.amount_cents, body.description, now)).fetchone()["id"]
        return {"id": tx_id, "status": "completed", **body.model_dump(), "created_at": now}

    return run_idempotent(db, client.api_key_id, idempotency_key, body.model_dump(), 201, handler)


# ---------- payouts (leaky bucket shaping) ----------

@app.post("/v1/payouts", status_code=202, tags=["banking"])
def create_payout(
    body: PayoutIn,
    request: Request,
    idempotency_key: str = Header("", alias="Idempotency-Key"),
    client: Client = Depends(metered_client),
    db: Database = Depends(get_db),
):
    """Queue a payout to an external bank.

    Payouts leave for the payment rail at a steady 1/second per customer. A
    batch is accepted immediately and each payout gets a `dispatch_at` time; if
    the queue (20) is full the request is rejected with 429.
    """
    def handler() -> dict:
        src = _own_account(db, client, body.from_account_id)
        if src["balance_cents"] < body.amount_cents:  # check before taking a slot in the queue
            raise HTTPException(422, {"error": "insufficient_funds", "balance_cents": src["balance_cents"]})
        decision = enforce(request, policies.limiters.payout_rail, f"cust:{client.customer_id}",
                           "Payout queue is full. Payouts are dispatched at 1/second.")
        now = time.time()
        payout_id = _queue_payout(db, src["id"], body.amount_cents, body.beneficiary, now, decision.delay)
        return {"id": payout_id, "status": "queued", **body.model_dump(),
                "queue_delay_seconds": round(decision.delay, 3), "dispatch_at": now + decision.delay}

    return run_idempotent(db, client.api_key_id, idempotency_key, body.model_dump(), 202, handler)


@app.post("/v1/payouts/batch", status_code=202, tags=["banking"])
def create_payout_batch(
    body: PayoutBatchIn,
    idempotency_key: str = Header("", alias="Idempotency-Key"),
    client: Client = Depends(metered_client),
    db: Database = Depends(get_db),
):
    """Submit a payroll-style batch (up to 100 payouts) in one API call.

    The call costs one token from the API bucket, but every payout enters the
    leaky bucket on its own: items that fit in the queue are accepted with
    staggered `dispatch_at` times, and the overflow is rejected per item so the
    client can resubmit just those later.
    """
    def handler() -> dict:
        src = _own_account(db, client, body.from_account_id)
        balance = src["balance_cents"]
        now = time.time()
        results = []
        for item in body.payouts:
            if item.amount_cents > balance:
                results.append({**item.model_dump(), "status": "rejected", "reason": "insufficient_funds"})
                continue
            decision = policies.limiters.payout_rail.acquire(f"cust:{client.customer_id}")
            if not decision.allowed:
                results.append({**item.model_dump(), "status": "rejected", "reason": "queue_full",
                                "retry_after_seconds": round(decision.retry_after, 3)})
                continue
            balance -= item.amount_cents
            payout_id = _queue_payout(db, src["id"], item.amount_cents, item.beneficiary, now, decision.delay)
            results.append({"id": payout_id, **item.model_dump(), "status": "queued",
                            "queue_delay_seconds": round(decision.delay, 3), "dispatch_at": now + decision.delay})
        accepted = sum(r["status"] == "queued" for r in results)
        return {"accepted": accepted, "rejected": len(results) - accepted, "payouts": results}

    return run_idempotent(db, client.api_key_id, idempotency_key, body.model_dump(), 202, handler)


@app.get("/v1/payouts", tags=["banking"])
def list_payouts(
    limit: int = Query(20, ge=1, le=100),
    cursor: Optional[str] = None,
    client: Client = Depends(metered_client),
    db: Database = Depends(get_db),
):
    before_id = decode_cursor(cursor)
    sql = ("SELECT p.* FROM payouts p JOIN accounts a ON a.id = p.account_id WHERE a.customer_id = ?"
           + (" AND p.id < ?" if before_id is not None else ""))
    args = (client.customer_id, before_id) if before_id is not None else (client.customer_id,)
    rows = db.execute(sql + " ORDER BY p.id DESC LIMIT ?", (*args, limit + 1)).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    now = time.time()
    data = [{**dict(r), "status": "dispatched" if r["dispatch_at"] <= now else "queued"} for r in rows]
    return {"data": data, "has_more": has_more,
            "next_cursor": encode_cursor(rows[-1]["id"]) if has_more else None}


# ---------- usage & dashboard ----------

@app.get("/v1/usage", tags=["usage"])
def get_usage(client: Client = Depends(management_client), db: Database = Depends(get_db)):
    return usage_report(db, client.customer_id, client.plan)


@app.get("/v1/limits", tags=["usage"])
def get_limits(client: Client = Depends(management_client), db: Database = Depends(get_db)):
    """Live state of every limiter that applies to this key, read with peek() so
    looking doesn't consume anything. Call it between other requests to watch the
    token bucket drain and refill, the transfer window slide, and the payout queue empty."""
    lim = policies.limiters
    plan = client.plan.name

    def state(limiter, key: str) -> dict:
        d = limiter.peek(key)
        return {**limiter.describe(), "key": key, "remaining": d.remaining,
                "next_request_allowed": d.allowed, "retry_after_seconds": round(d.retry_after, 3),
                "reset_after_seconds": round(d.reset_after, 3)}

    account_ids = [r["id"] for r in db.execute("SELECT id FROM accounts WHERE customer_id = ? ORDER BY id",
                                               (client.customer_id,))]
    return {
        "api_traffic": state(lim.api_traffic[plan], f"key:{client.api_key_id}"),
        "daily_quota": state(lim.daily_quota[plan], f"cust:{client.customer_id}"),
        "transfer_velocity": [state(lim.transfer_velocity, f"acct:{a}") for a in account_ids],
        "payout_rail": state(lim.payout_rail, f"cust:{client.customer_id}"),
    }


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/dashboard")


@app.get("/dashboard", include_in_schema=False)
def dashboard():
    return FileResponse(STATIC / "dashboard.html")


@app.get("/guide", include_in_schema=False)
def guide():
    return FileResponse(STATIC / "guide.html")
