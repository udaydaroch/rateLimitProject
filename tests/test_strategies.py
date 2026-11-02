import pytest

from app.ratelimit import (
    STRATEGIES,
    FixedWindow,
    LeakyBucket,
    RateLimiter,
    SlidingWindowLog,
    TokenBucket,
    create_strategy,
)


class FakeClock:
    def __init__(self, now: float = 1_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


def allowed(strategy, key, n):
    return [strategy.acquire(key).allowed for _ in range(n)]


# ---- token bucket ----

def test_token_bucket_allows_burst_then_refills(clock):
    tb = TokenBucket(capacity=5, refill_rate=1, clock=clock)
    assert allowed(tb, "k", 6) == [True] * 5 + [False]

    denied = tb.acquire("k")
    assert denied.retry_after == pytest.approx(1.0)

    clock.advance(2)
    assert allowed(tb, "k", 3) == [True, True, False]


def test_token_bucket_never_exceeds_capacity(clock):
    tb = TokenBucket(capacity=3, refill_rate=10, clock=clock)
    clock.advance(1_000)
    assert allowed(tb, "k", 4) == [True, True, True, False]


def test_token_bucket_keys_are_independent(clock):
    tb = TokenBucket(capacity=1, refill_rate=1, clock=clock)
    assert tb.acquire("a").allowed and tb.acquire("b").allowed
    assert not tb.acquire("a").allowed


# ---- sliding window ----

def test_sliding_window_blocks_boundary_burst(clock):
    clock.now = 59.0  # just before a minute boundary
    sw = SlidingWindowLog(limit=5, window=60, clock=clock)
    assert allowed(sw, "acct", 5) == [True] * 5

    clock.advance(1.5)  # crosses the minute boundary; a fixed window would reset here
    decision = sw.acquire("acct")
    assert not decision.allowed
    assert decision.retry_after == pytest.approx(58.5)

    clock.advance(58.5)  # first five timestamps slide out
    assert sw.acquire("acct").allowed


def test_fixed_window_allows_boundary_burst(clock):
    """The weakness that makes fixed windows wrong for transfer velocity."""
    clock.now = 59.0
    fw = FixedWindow(limit=5, window=60, clock=clock)
    assert allowed(fw, "acct", 5) == [True] * 5
    clock.advance(1.5)
    assert allowed(fw, "acct", 5) == [True] * 5  # 10 requests within 1.5 seconds


# ---- fixed window ----

def test_fixed_window_resets_on_boundary(clock):
    clock.now = 120.0
    fw = FixedWindow(limit=2, window=60, clock=clock)
    assert allowed(fw, "c", 3) == [True, True, False]
    d = fw.acquire("c")
    assert d.retry_after == pytest.approx(60.0)
    clock.advance(60)
    assert fw.acquire("c").allowed


# ---- leaky bucket ----

def test_leaky_bucket_queues_with_increasing_delay(clock):
    lb = LeakyBucket(capacity=3, leak_rate=1, clock=clock)
    delays = [lb.acquire("cust").delay for _ in range(3)]
    assert delays == pytest.approx([0, 1, 2])  # dispatched one per second
    overflow = lb.acquire("cust")
    assert not overflow.allowed
    assert overflow.retry_after == pytest.approx(1.0)

    clock.advance(1)
    assert lb.acquire("cust").delay == pytest.approx(2)


# ---- peek (shared interface) ----

@pytest.mark.parametrize("strategy_factory", [
    lambda c: TokenBucket(capacity=2, refill_rate=1, clock=c),
    lambda c: SlidingWindowLog(limit=2, window=60, clock=c),
    lambda c: FixedWindow(limit=2, window=60, clock=c),
    lambda c: LeakyBucket(capacity=2, leak_rate=1, clock=c),
])
def test_peek_never_consumes(clock, strategy_factory):
    s = strategy_factory(clock)
    for _ in range(10):
        assert s.peek("k").allowed
    assert allowed(s, "k", 3) == [True, True, False]
    assert not s.peek("k").allowed


# ---- strategy pattern: context, swap, factory ----

def test_context_delegates_and_namespaces_keys(clock):
    strategy = FixedWindow(limit=1, window=60, clock=clock)
    a = RateLimiter("a", strategy)
    b = RateLimiter("b", strategy)  # same strategy object, different namespaces
    assert a.acquire("x").allowed
    assert b.acquire("x").allowed
    assert not a.acquire("x").allowed


def test_context_can_swap_strategy_at_runtime(clock):
    limiter = RateLimiter("transfers", FixedWindow(limit=5, window=60, clock=clock))
    assert limiter.describe()["algorithm"] == "fixed_window"

    limiter.set_strategy(SlidingWindowLog(limit=1, window=60, clock=clock))
    assert limiter.describe() == {"name": "transfers", "algorithm": "sliding_window",
                                  "limit": 1, "window_seconds": 60}
    assert limiter.acquire("acct").allowed
    assert not limiter.acquire("acct").allowed


def test_factory_builds_every_registered_strategy():
    params = {
        "token_bucket": {"capacity": 1, "refill_rate": 1},
        "sliding_window": {"limit": 1, "window": 1},
        "fixed_window": {"limit": 1, "window": 1},
        "leaky_bucket": {"capacity": 1, "leak_rate": 1},
    }
    assert set(params) == set(STRATEGIES)
    for name, p in params.items():
        assert create_strategy(name, **p).policy == name


def test_factory_rejects_unknown_algorithm():
    with pytest.raises(ValueError, match="unknown rate limit algorithm"):
        create_strategy("magic_bucket")
