"""Strategy interface shared by every rate limiting algorithm.

Strategy pattern roles:
  * RateLimitStrategy (this file)   - the strategy interface
  * TokenBucket, SlidingWindowLog,
    FixedWindow, LeakyBucket        - concrete strategies (one algorithm each)
  * RateLimiter (limiter.py)        - the context the app talks to; it holds a
                                      strategy and can swap it at runtime
  * create_strategy (factory.py)    - builds a strategy from config by name

Strategies take an injectable clock so tests can control time exactly.
"""
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Dict

Clock = Callable[[], float]


@dataclass(frozen=True)
class Decision:
    allowed: bool
    policy: str           # algorithm that made the decision, e.g. "token_bucket"
    limit: int            # max requests the policy allows (capacity / window limit)
    remaining: int        # requests still available right now
    reset_after: float    # seconds until the limiter is fully replenished / window resets
    retry_after: float    # seconds the caller must wait before retrying (0 when allowed)
    delay: float = 0.0    # leaky bucket only: how long this request waits in the queue


class RateLimitStrategy(ABC):
    policy = "base"

    def __init__(self, clock: Clock = time.time):
        self._clock = clock
        self._lock = threading.Lock()

    def acquire(self, key: str, cost: int = 1) -> Decision:
        """Try to consume `cost` units for `key`. Never blocks."""
        with self._lock:
            return self._evaluate(key, cost, consume=True)

    def peek(self, key: str, cost: int = 1) -> Decision:
        """What `acquire` would decide right now, without consuming anything."""
        with self._lock:
            return self._evaluate(key, cost, consume=False)

    @abstractmethod
    def _evaluate(self, key: str, cost: int, consume: bool) -> Decision:
        """The algorithm itself. Called with the lock held."""

    @abstractmethod
    def reset(self, key: str) -> None:
        """Forget all state for `key`."""

    @abstractmethod
    def describe(self) -> Dict:
        """Public, algorithm-specific parameters (for docs and dashboards)."""
