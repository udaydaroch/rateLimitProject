"""ASGI middleware that adds rate limit headers and meters usage.

Runs around every request, so it sees the final status code, including 429s
raised from dependencies, and records one usage event per authenticated call.
"""
import math
import time

import anyio
from starlette.datastructures import MutableHeaders

from .db import connect


def _record_usage(api_key_id: int, customer_id: int, endpoint: str, status: int) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO usage_events (api_key_id, customer_id, endpoint, status_code, billable, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (api_key_id, customer_id, endpoint, status, int(status < 400), time.time()),
        )
    finally:
        conn.close()


class RateLimitHeadersAndMetering:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        status_holder = {"status": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                _apply_headers(scope.get("state", {}), MutableHeaders(scope=message), message["status"])
            await send(message)

        await self.app(scope, receive, send_wrapper)

        state = scope.get("state", {})
        if "api_key_id" in state and not state.get("unmetered"):
            route = scope.get("route")
            endpoint = f"{scope['method']} {route.path if route else scope['path']}"
            await anyio.to_thread.run_sync(
                _record_usage, state["api_key_id"], state["customer_id"], endpoint, status_holder["status"]
            )


def _apply_headers(state: dict, headers: MutableHeaders, status: int) -> None:
    decisions = state.get("rl_decisions") or []
    if decisions:
        denied = [d for d in decisions if not d.allowed]
        # Report the limit that blocked the call, otherwise the one closest to blocking.
        d = denied[0] if denied else min(decisions, key=lambda x: x.remaining)
        headers["RateLimit-Policy"] = d.policy
        headers["RateLimit-Limit"] = str(d.limit)
        headers["RateLimit-Remaining"] = str(d.remaining)
        headers["RateLimit-Reset"] = str(math.ceil(d.reset_after))
        if status == 429:
            headers["Retry-After"] = str(max(1, math.ceil(d.retry_after)))
    if "quota_remaining" in state:
        headers["X-Daily-Quota-Remaining"] = str(state["quota_remaining"])
    if state.get("quota_overage"):
        headers["X-Quota-Overage"] = "true"
