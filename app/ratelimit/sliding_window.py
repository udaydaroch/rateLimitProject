"""Sliding window log strategy.

Keeps the timestamp of every accepted request in the last `window` seconds.
A request is allowed only if fewer than `limit` timestamps are inside the
window ending *now*.

Banking use: transfer velocity checks per source account ("at most 5 outgoing
transfers in any 60 second period"). This is a fraud/compromise control, so it
must be exact. A fixed window would let an attacker send 5 transfers at
12:00:59 and 5 more at 12:01:00 (10 in two seconds). The sliding window has no
such boundary to exploit.

Memory is O(limit) per key, which is fine because velocity limits are small.
"""
import time
from collections import deque
from typing import Deque, Dict

from .base import Clock, Decision, RateLimitStrategy


class SlidingWindowLog(RateLimitStrategy):
    policy = "sliding_window"

    def __init__(self, limit: int, window: float, clock: Clock = time.time):
        super().__init__(clock)
        if limit <= 0 or window <= 0:
            raise ValueError("limit and window must be positive")
        self.limit = limit
        self.window = window
        self._logs: Dict[str, Deque[float]] = {}

    def _evaluate(self, key: str, cost: int, consume: bool) -> Decision:
        now = self._clock()
        log = self._logs.setdefault(key, deque())
        while log and log[0] <= now - self.window:
            log.popleft()

        allowed = len(log) + cost <= self.limit
        if consume and allowed:
            log.extend([now] * cost)

        if allowed or not log:
            retry_after = 0.0
        else:
            # Wait until enough old entries slide out to make room for `cost`.
            idx = min(len(log) + cost - self.limit, len(log)) - 1
            retry_after = log[idx] + self.window - now

        return Decision(
            allowed=allowed,
            policy=self.policy,
            limit=self.limit,
            remaining=max(0, self.limit - len(log)),
            reset_after=(log[-1] + self.window - now) if log else 0.0,
            retry_after=max(0.0, retry_after),
        )

    def reset(self, key: str) -> None:
        with self._lock:
            self._logs.pop(key, None)

    def describe(self) -> Dict:
        return {"algorithm": self.policy, "limit": self.limit, "window_seconds": self.window}
