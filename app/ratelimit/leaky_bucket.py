"""Leaky bucket strategy (as a queue / traffic shaper).

Requests pour into a bucket of size `capacity` that drains at a constant
`leak_rate` per second. If the bucket would overflow, the request is rejected.
Accepted requests are released downstream at a perfectly smooth rate, and the
Decision tells the caller how long its request waits in the queue (`delay`).

Banking use: outbound payouts to the clearing network / payment rail. The
downstream system (ACH, Faster Payments, a partner bank) only accepts N
messages per second and penalises spikes. A partner submitting a payroll batch
of 20 payouts at once has them accepted immediately but *dispatched* one per
second. Unlike the token bucket, the leaky bucket never lets a burst through
downstream; it smooths output rather than just capping it.
"""
import time
from typing import Dict, Tuple

from .base import Clock, Decision, RateLimitStrategy


class LeakyBucket(RateLimitStrategy):
    policy = "leaky_bucket"

    def __init__(self, capacity: int, leak_rate: float, clock: Clock = time.time):
        super().__init__(clock)
        if capacity <= 0 or leak_rate <= 0:
            raise ValueError("capacity and leak_rate must be positive")
        self.capacity = capacity
        self.leak_rate = leak_rate
        self._buckets: Dict[str, Tuple[float, float]] = {}  # key -> (level, last_leak)

    def _evaluate(self, key: str, cost: int, consume: bool) -> Decision:
        now = self._clock()
        level, last = self._buckets.get(key, (0.0, now))
        level = max(0.0, level - (now - last) * self.leak_rate)

        allowed = level + cost <= self.capacity
        # Everything already queued must drain before this request goes out.
        delay = level / self.leak_rate if allowed else 0.0
        if consume:
            if allowed:
                level += cost
            self._buckets[key] = (level, now)

        return Decision(
            allowed=allowed,
            policy=self.policy,
            limit=self.capacity,
            remaining=int(self.capacity - level),
            reset_after=level / self.leak_rate,
            retry_after=0.0 if allowed else (level + cost - self.capacity) / self.leak_rate,
            delay=delay,
        )

    def reset(self, key: str) -> None:
        with self._lock:
            self._buckets.pop(key, None)

    def describe(self) -> Dict:
        return {"algorithm": self.policy, "queue_capacity": self.capacity, "dispatch_per_second": self.leak_rate}
