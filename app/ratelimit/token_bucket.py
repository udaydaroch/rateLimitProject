"""Token bucket strategy.

A bucket holds up to `capacity` tokens and refills continuously at
`refill_rate` tokens/second. Each request spends a token.

Banking use: general read traffic per API key (balances, account lists,
transaction history). A mobile banking app fires a burst of calls when the user
opens it, then goes quiet. Token bucket allows that burst (up to `capacity`)
while still capping the long-run average at `refill_rate`.
"""
import time
from typing import Dict, Tuple

from .base import Clock, Decision, RateLimitStrategy


class TokenBucket(RateLimitStrategy):
    policy = "token_bucket"

    def __init__(self, capacity: int, refill_rate: float, clock: Clock = time.time):
        super().__init__(clock)
        if capacity <= 0 or refill_rate <= 0:
            raise ValueError("capacity and refill_rate must be positive")
        self.capacity = capacity
        self.refill_rate = refill_rate
        self._buckets: Dict[str, Tuple[float, float]] = {}  # key -> (tokens, last_refill)

    def _evaluate(self, key: str, cost: int, consume: bool) -> Decision:
        now = self._clock()
        tokens, last = self._buckets.get(key, (float(self.capacity), now))
        tokens = min(self.capacity, tokens + (now - last) * self.refill_rate)

        allowed = tokens >= cost
        if consume:
            if allowed:
                tokens -= cost
            self._buckets[key] = (tokens, now)

        return Decision(
            allowed=allowed,
            policy=self.policy,
            limit=self.capacity,
            remaining=int(tokens),
            reset_after=(self.capacity - tokens) / self.refill_rate,
            retry_after=0.0 if allowed else (cost - tokens) / self.refill_rate,
        )

    def reset(self, key: str) -> None:
        with self._lock:
            self._buckets.pop(key, None)

    def describe(self) -> Dict:
        return {"algorithm": self.policy, "burst": self.capacity, "refill_per_second": self.refill_rate}
