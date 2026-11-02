"""Builds a strategy from a config name, so policies are data, not code.

    create_strategy("sliding_window", limit=5, window=60)
"""
from typing import Dict, Type

from .base import RateLimitStrategy
from .fixed_window import FixedWindow
from .leaky_bucket import LeakyBucket
from .sliding_window import SlidingWindowLog
from .token_bucket import TokenBucket

STRATEGIES: Dict[str, Type[RateLimitStrategy]] = {
    TokenBucket.policy: TokenBucket,
    SlidingWindowLog.policy: SlidingWindowLog,
    FixedWindow.policy: FixedWindow,
    LeakyBucket.policy: LeakyBucket,
}


def create_strategy(algorithm: str, **params) -> RateLimitStrategy:
    try:
        cls = STRATEGIES[algorithm]
    except KeyError:
        raise ValueError(f"unknown rate limit algorithm {algorithm!r}; choose from {sorted(STRATEGIES)}")
    return cls(**params)
