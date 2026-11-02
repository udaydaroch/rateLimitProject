"""Fixed window counter strategy.

Time is cut into aligned windows (e.g. each UTC day, each minute). A counter
per (key, window) is incremented per request and resets when the window rolls.

Banking use:
  * Daily API quota per plan (free: 1,000 calls/day, resetting at 00:00 UTC).
    Quotas are a billing/contract concept, and customers need a reset time they
    can predict and put on an invoice. Fixed windows give exactly that.
  * Failed-authentication lockout per IP (brute-forcing API keys). Cheap, one
    counter per IP, and boundary bursts don't matter at this scale.
"""
import time
from typing import Dict, Tuple

from .base import Clock, Decision, RateLimitStrategy


class FixedWindow(RateLimitStrategy):
    policy = "fixed_window"

    def __init__(self, limit: int, window: float, clock: Clock = time.time):
        super().__init__(clock)
        if limit <= 0 or window <= 0:
            raise ValueError("limit and window must be positive")
        self.limit = limit
        self.window = window
        self._counters: Dict[str, Tuple[int, int]] = {}  # key -> (window_id, count)

    def _evaluate(self, key: str, cost: int, consume: bool) -> Decision:
        now = self._clock()
        window_id = int(now // self.window)
        current_id, count = self._counters.get(key, (window_id, 0))
        if current_id != window_id:
            count = 0

        allowed = count + cost <= self.limit
        if consume:
            if allowed:
                count += cost
            self._counters[key] = (window_id, count)

        reset_after = (window_id + 1) * self.window - now
        return Decision(
            allowed=allowed,
            policy=self.policy,
            limit=self.limit,
            remaining=max(0, self.limit - count),
            reset_after=reset_after,
            retry_after=0.0 if allowed else reset_after,
        )

    def reset(self, key: str) -> None:
        with self._lock:
            self._counters.pop(key, None)

    def describe(self) -> Dict:
        return {"algorithm": self.policy, "limit": self.limit, "window_seconds": self.window}
