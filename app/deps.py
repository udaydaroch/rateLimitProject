"""Authentication and rate limit enforcement, wired in as FastAPI dependencies.

Every decision made for a request is stored on `request.state.rl_decisions`;
the middleware turns those into RateLimit-* response headers.
"""
import os
import time
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from . import policies
from .db import Database, get_db
from .policies import PLANS, Plan
from .ratelimit import Decision, RateLimiter
from .security import parse_api_key, verify_secret


@dataclass(frozen=True)
class Client:
    api_key_id: int
    customer_id: int
    plan: Plan


def record(request: Request, decision: Decision) -> None:
    if not hasattr(request.state, "rl_decisions"):
        request.state.rl_decisions = []
    request.state.rl_decisions.append(decision)


def enforce(request: Request, limiter: RateLimiter, key: str, message: str) -> Decision:
    decision = limiter.acquire(key)
    record(request, decision)
    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "rate_limited",
                "policy": decision.policy,
                "message": message,
                "retry_after_seconds": round(decision.retry_after, 3),
            },
        )
    return decision


# Behind Vercel's proxy every request arrives from the proxy itself. Vercel overwrites
# X-Forwarded-For with the real client address, so it's safe to trust there.
TRUST_PROXY = bool(os.environ.get("VERCEL") or os.environ.get("TRUST_PROXY"))

# Declared as a security scheme so Swagger (/docs) shows an "Authorize" button.
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False,
                              description="Your key from POST /v1/signup, e.g. bk_live_xxxxxxxx_…")


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if TRUST_PROXY and forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def authenticate(
    request: Request,
    x_api_key: Optional[str] = Security(api_key_header),
    db: Database = Depends(get_db),
) -> Client:
    ip = client_ip(request)
    lockout = policies.limiters.auth_failures
    status = lockout.peek(ip)
    if not status.allowed:
        record(request, status)
        raise HTTPException(
            status_code=429,
            detail={"error": "too_many_auth_failures", "policy": status.policy,
                    "retry_after_seconds": round(status.retry_after, 3)},
        )

    raw = x_api_key or ""
    auth = request.headers.get("authorization", "")
    if not raw and auth.lower().startswith("bearer "):
        raw = auth[7:].strip()

    parsed = parse_api_key(raw)
    row = None
    if parsed:
        row = db.execute(
            "SELECT k.id, k.key_hash, k.revoked_at, c.id AS customer_id, c.plan "
            "FROM api_keys k JOIN customers c ON c.id = k.customer_id WHERE k.prefix = ?",
            (parsed[0],),
        ).fetchone()

    if row is None or row["revoked_at"] is not None or not verify_secret(parsed[1], row["key_hash"]):
        lockout.acquire(ip)
        raise HTTPException(status_code=401, detail={"error": "invalid_api_key"})

    db.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (time.time(), row["id"]))
    request.state.api_key_id = row["id"]
    request.state.customer_id = row["customer_id"]
    return Client(api_key_id=row["id"], customer_id=row["customer_id"], plan=PLANS[row["plan"]])


def management_client(request: Request, client: Client = Depends(authenticate)) -> Client:
    """Dashboard / account management calls (usage, keys): authenticated, but not
    rate limited or billed, so checking your usage never eats into your API limits."""
    request.state.unmetered = True
    return client


def metered_client(request: Request, client: Client = Depends(authenticate)) -> Client:
    """Applies the two limits every authenticated call is subject to."""
    plan = client.plan

    # 1. Short-term burst control, per API key (token bucket).
    enforce(request, policies.limiters.api_traffic[plan.name], f"key:{client.api_key_id}",
            f"Too many requests for this API key. The {plan.name} plan allows bursts of "
            f"{plan.burst} and {plan.refill_per_sec:g} requests/second sustained.")

    # 2. Daily quota, per customer (fixed window, resets 00:00 UTC).
    quota = policies.limiters.daily_quota[plan.name]
    decision = quota.acquire(f"cust:{client.customer_id}")
    request.state.quota_remaining = decision.remaining
    if not decision.allowed:
        if plan.allow_overage:
            request.state.quota_overage = True  # served, billed at the overage rate
        else:
            record(request, decision)
            raise HTTPException(
                status_code=429,
                detail={"error": "daily_quota_exceeded", "policy": decision.policy,
                        "message": f"The {plan.name} plan includes {plan.daily_quota} calls/day. "
                                   "Upgrade to pro for overage billing.",
                        "retry_after_seconds": round(decision.retry_after, 3)},
            )
    return client
