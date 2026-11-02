"""Builds a strategy from a config name, so policies are data, not code.

    create_strategy("sliding_window", limit=5, window=60)                      # in-process
    create_strategy("sliding_window", backend="redis", redis=client, limit=5, window=60)

Choosing the backend is itself a strategy choice: the same algorithm can keep
its state in process memory (one server, tests) or in Redis (many servers).
"""
from typing import Dict, Type

from .base import RateLimitStrategy
from .fixed_window import FixedWindow
from .leaky_bucket import LeakyBucket
from .redis_strategies import RedisFixedWindow, RedisLeakyBucket, RedisSlidingWindowLog, RedisTokenBucket
from .sliding_window import SlidingWindowLog
from .token_bucket import TokenBucket

STRATEGIES: Dict[str, Type[RateLimitStrategy]] = {
    TokenBucket.policy: TokenBucket,
    SlidingWindowLog.policy: SlidingWindowLog,
    FixedWindow.policy: FixedWindow,
    LeakyBucket.policy: LeakyBucket,
}

REDIS_STRATEGIES: Dict[str, Type[RateLimitStrategy]] = {
    RedisTokenBucket.policy: RedisTokenBucket,
    RedisSlidingWindowLog.policy: RedisSlidingWindowLog,
    RedisFixedWindow.policy: RedisFixedWindow,
    RedisLeakyBucket.policy: RedisLeakyBucket,
}


def create_strategy(algorithm: str, backend: str = "memory", redis=None, **params) -> RateLimitStrategy:
    registry = {"memory": STRATEGIES, "redis": REDIS_STRATEGIES}.get(backend)
    if registry is None:
        raise ValueError(f"unknown backend {backend!r}; choose 'memory' or 'redis'")
    try:
        cls = registry[algorithm]
    except KeyError:
        raise ValueError(f"unknown rate limit algorithm {algorithm!r}; choose from {sorted(registry)}")
    if backend == "redis":
        if redis is None:
            raise ValueError("the redis backend needs a redis client")
        return cls(redis, **params)
    return cls(**params)
