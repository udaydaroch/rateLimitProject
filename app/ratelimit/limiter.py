"""RateLimiter: the Strategy pattern *context*.

Application code only ever talks to a RateLimiter. It doesn't know or care
which algorithm is behind it, so a policy can move from fixed window to sliding
window (in config, or live via `set_strategy`) without touching any endpoint.
"""
from typing import Dict

from .base import Decision, RateLimitStrategy


class RateLimiter:
    def __init__(self, name: str, strategy: RateLimitStrategy):
        self.name = name
        self._strategy = strategy

    @property
    def strategy(self) -> RateLimitStrategy:
        return self._strategy

    def set_strategy(self, strategy: RateLimitStrategy) -> None:
        """Swap the algorithm at runtime. State held by the old strategy is dropped."""
        self._strategy = strategy

    def _key(self, key: str) -> str:
        return f"{self.name}:{key}"

    def acquire(self, key: str, cost: int = 1) -> Decision:
        return self._strategy.acquire(self._key(key), cost)

    def peek(self, key: str, cost: int = 1) -> Decision:
        return self._strategy.peek(self._key(key), cost)

    def reset(self, key: str) -> None:
        self._strategy.reset(self._key(key))

    def describe(self) -> Dict:
        return {"name": self.name, **self._strategy.describe()}
