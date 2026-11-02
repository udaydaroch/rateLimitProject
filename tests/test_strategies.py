import fakeredis
import pytest

from app.ratelimit import (
    REDIS_STRATEGIES,
    STRATEGIES,
    FixedWindow,
    RateLimiter,
    SlidingWindowLog,
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


@pytest.fixture(params=["memory", "redis"])
def make(request, clock):
    """Builds a strategy on each backend: every algorithm test runs against both
    the in-process implementation and the Redis + Lua implementation."""
    redis = fakeredis.FakeRedis() if request.param == "redis" else None

    def build(algorithm, **params):
        return create_strategy(algorithm, backend=request.param, redis=redis, clock=clock, **params)

    return build


def allowed(strategy, key, n):
    return [strategy.acquire(key).allowed for _ in range(n)]


# ---- token bucket ----

def test_token_bucket_allows_burst_then_refills(make, clock):
    tb = make("token_bucket", capacity=5, refill_rate=1)
    assert allowed(tb, "k", 6) == [True] * 5 + [False]

    denied = tb.acquire("k")
    assert denied.retry_after == pytest.approx(1.0)

    clock.advance(2)
    assert allowed(tb, "k", 3) == [True, True, False]


def test_token_bucket_never_exceeds_capacity(make, clock):
    tb = make("token_bucket", capacity=3, refill_rate=10)
    clock.advance(1_000)
    assert allowed(tb, "k", 4) == [True, True, True, False]


def test_token_bucket_keys_are_independent(make):
    tb = make("token_bucket", capacity=1, refill_rate=1)
    assert tb.acquire("a").allowed and tb.acquire("b").allowed
    assert not tb.acquire("a").allowed


# ---- sliding window ----

def test_sliding_window_blocks_boundary_burst(make, clock):
    clock.now = 59.0  # just before a minute boundary
    sw = make("sliding_window", limit=5, window=60)
    assert allowed(sw, "acct", 5) == [True] * 5

    clock.advance(1.5)  # crosses the minute boundary; a fixed window would reset here
    decision = sw.acquire("acct")
    assert not decision.allowed
    assert decision.retry_after == pytest.approx(58.5)

    clock.advance(58.5)  # first five timestamps slide out
    assert sw.acquire("acct").allowed


def test_fixed_window_allows_boundary_burst(make, clock):
    """The weakness that makes fixed windows wrong for transfer velocity."""
    clock.now = 59.0
    fw = make("fixed_window", limit=5, window=60)
    assert allowed(fw, "acct", 5) == [True] * 5
    clock.advance(1.5)
    assert allowed(fw, "acct", 5) == [True] * 5  # 10 requests within 1.5 seconds


# ---- fixed window ----

def test_fixed_window_resets_on_boundary(make, clock):
    clock.now = 120.0
    fw = make("fixed_window", limit=2, window=60)
    assert allowed(fw, "c", 3) == [True, True, False]
    d = fw.acquire("c")
    assert d.retry_after == pytest.approx(60.0)
    clock.advance(60)
    assert fw.acquire("c").allowed


# ---- leaky bucket ----

def test_leaky_bucket_queues_with_increasing_delay(make, clock):
    lb = make("leaky_bucket", capacity=3, leak_rate=1)
    delays = [lb.acquire("cust").delay for _ in range(3)]
    assert delays == pytest.approx([0, 1, 2])  # dispatched one per second
    overflow = lb.acquire("cust")
    assert not overflow.allowed
    assert overflow.retry_after == pytest.approx(1.0)

    clock.advance(1)
    assert lb.acquire("cust").delay == pytest.approx(2)


# ---- peek (shared interface) ----

@pytest.mark.parametrize("algorithm,params", [
    ("token_bucket", {"capacity": 2, "refill_rate": 1}),
    ("sliding_window", {"limit": 2, "window": 60}),
    ("fixed_window", {"limit": 2, "window": 60}),
    ("leaky_bucket", {"capacity": 2, "leak_rate": 1}),
])
def test_peek_never_consumes(make, algorithm, params):
    s = make(algorithm, **params)
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
    assert set(params) == set(STRATEGIES) == set(REDIS_STRATEGIES)
    redis = fakeredis.FakeRedis()
    for name, p in params.items():
        assert create_strategy(name, **p).policy == name
        assert create_strategy(name, backend="redis", redis=redis, **p).policy == name


def test_factory_rejects_unknown_algorithm():
    with pytest.raises(ValueError, match="unknown rate limit algorithm"):
        create_strategy("magic_bucket")


def test_redis_strategies_share_state_across_instances(clock):
    """Two app instances (two strategy objects) on one Redis see the same bucket,
    which is the whole reason for the Redis backend on serverless hosting."""
    redis = fakeredis.FakeRedis()
    instance_a = create_strategy("token_bucket", backend="redis", redis=redis, clock=clock, capacity=3, refill_rate=1)
    instance_b = create_strategy("token_bucket", backend="redis", redis=redis, clock=clock, capacity=3, refill_rate=1)
    assert [instance_a.acquire("k").allowed, instance_b.acquire("k").allowed,
            instance_a.acquire("k").allowed, instance_b.acquire("k").allowed] == [True, True, True, False]


def test_reset_clears_redis_state(clock):
    fw = create_strategy("fixed_window", backend="redis", redis=fakeredis.FakeRedis(), clock=clock,
                         limit=1, window=60)
    assert fw.acquire("k").allowed and not fw.acquire("k").allowed
    fw.reset("k")
    assert fw.acquire("k").allowed
