"""Redis-backed versions of the four strategies.

In-memory strategies only work inside one process. On Vercel, requests are
spread across many short-lived instances, so the limiter state has to live in
a shared store. Each strategy here runs its whole read-decide-write cycle as
a single Lua script, which Redis executes atomically: two instances can never
both read "1 token left" and both spend it.

The algorithms are identical to the in-memory ones. The Lua script returns the
raw state (tokens, counts, timestamps) and Python builds the Decision with the
same formulas, so both backends pass the same test suite. Floats travel as
strings because Redis truncates Lua numbers to integers in replies. `now` comes
from the app (an injectable clock, NTP-synced in production), so tests can
control time exactly.
"""
import time
import uuid
from typing import Dict

from .base import Clock, Decision, RateLimitStrategy

PREFIX = "rl:"

TOKEN_BUCKET_LUA = """
local capacity = tonumber(ARGV[1])
local rate     = tonumber(ARGV[2])
local now      = tonumber(ARGV[3])
local cost     = tonumber(ARGV[4])
local consume  = ARGV[5] == '1'

local state  = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1]) or capacity
local last   = tonumber(state[2]) or now
tokens = math.min(capacity, tokens + math.max(0, now - last) * rate)

local allowed = tokens >= cost
if consume then
  if allowed then tokens = tokens - cost end
  redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now))
  redis.call('PEXPIRE', KEYS[1], math.ceil(capacity / rate * 1000) + 1000)
end
return {allowed and 1 or 0, tostring(tokens)}
"""

SLIDING_WINDOW_LUA = """
local limit   = tonumber(ARGV[1])
local window  = tonumber(ARGV[2])
local now     = tonumber(ARGV[3])
local cost    = tonumber(ARGV[4])
local consume = ARGV[5] == '1'
local member  = ARGV[6]

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
local count = redis.call('ZCARD', KEYS[1])
local allowed = count + cost <= limit
if consume and allowed then
  for i = 1, cost do redis.call('ZADD', KEYS[1], now, member .. ':' .. i) end
  count = count + cost
  redis.call('PEXPIRE', KEYS[1], math.ceil(window * 1000))
end

-- Timestamp of the entry that must expire before `cost` more fit (for retry_after).
local blocking = ''
if not allowed and count > 0 then
  local idx = math.min(count + cost - limit, count) - 1
  blocking = redis.call('ZRANGE', KEYS[1], idx, idx, 'WITHSCORES')[2]
end
local newest = redis.call('ZRANGE', KEYS[1], -1, -1, 'WITHSCORES')[2] or ''
return {allowed and 1 or 0, count, blocking, newest}
"""

FIXED_WINDOW_LUA = """
local limit   = tonumber(ARGV[1])
local cost    = tonumber(ARGV[2])
local consume = ARGV[3] == '1'
local ttl_ms  = tonumber(ARGV[4])

local count = tonumber(redis.call('GET', KEYS[1]) or '0')
local allowed = count + cost <= limit
if consume and allowed then
  count = redis.call('INCRBY', KEYS[1], cost)
  redis.call('PEXPIRE', KEYS[1], ttl_ms)
end
return {allowed and 1 or 0, count}
"""

LEAKY_BUCKET_LUA = """
local capacity = tonumber(ARGV[1])
local rate     = tonumber(ARGV[2])
local now      = tonumber(ARGV[3])
local cost     = tonumber(ARGV[4])
local consume  = ARGV[5] == '1'

local state = redis.call('HMGET', KEYS[1], 'level', 'ts')
local level = tonumber(state[1]) or 0
local last  = tonumber(state[2]) or now
level = math.max(0, level - math.max(0, now - last) * rate)

local allowed = level + cost <= capacity
local before = level
if consume then
  if allowed then level = level + cost end
  redis.call('HSET', KEYS[1], 'level', tostring(level), 'ts', tostring(now))
  redis.call('PEXPIRE', KEYS[1], math.ceil(level / rate * 1000) + 1000)
end
return {allowed and 1 or 0, tostring(before), tostring(level)}
"""


class _RedisStrategy(RateLimitStrategy):
    script_source = ""

    def __init__(self, client, clock: Clock = time.time):
        super().__init__(clock)
        self._redis = client
        self._script = client.register_script(self.script_source)

    def _key(self, key: str) -> str:
        return f"{PREFIX}{self.policy}:{key}"

    def reset(self, key: str) -> None:
        self._redis.delete(self._key(key))


class RedisTokenBucket(_RedisStrategy):
    policy = "token_bucket"
    script_source = TOKEN_BUCKET_LUA

    def __init__(self, client, capacity: int, refill_rate: float, clock: Clock = time.time):
        if capacity <= 0 or refill_rate <= 0:
            raise ValueError("capacity and refill_rate must be positive")
        super().__init__(client, clock)
        self.capacity = capacity
        self.refill_rate = refill_rate

    def _evaluate(self, key: str, cost: int, consume: bool) -> Decision:
        allowed, tokens = self._script(
            keys=[self._key(key)],
            args=[self.capacity, self.refill_rate, self._clock(), cost, int(consume)],
        )
        allowed, tokens = bool(allowed), float(tokens)
        return Decision(
            allowed=allowed,
            policy=self.policy,
            limit=self.capacity,
            remaining=int(tokens),
            reset_after=(self.capacity - tokens) / self.refill_rate,
            retry_after=0.0 if allowed else (cost - tokens) / self.refill_rate,
        )

    def describe(self) -> Dict:
        return {"algorithm": self.policy, "burst": self.capacity, "refill_per_second": self.refill_rate}


class RedisSlidingWindowLog(_RedisStrategy):
    policy = "sliding_window"
    script_source = SLIDING_WINDOW_LUA

    def __init__(self, client, limit: int, window: float, clock: Clock = time.time):
        if limit <= 0 or window <= 0:
            raise ValueError("limit and window must be positive")
        super().__init__(client, clock)
        self.limit = limit
        self.window = window

    def _evaluate(self, key: str, cost: int, consume: bool) -> Decision:
        now = self._clock()
        allowed, count, blocking, newest = self._script(
            keys=[self._key(key)],
            args=[self.limit, self.window, now, cost, int(consume), uuid.uuid4().hex],
        )
        allowed = bool(allowed)
        retry_after = 0.0 if allowed or not blocking else float(blocking) + self.window - now
        return Decision(
            allowed=allowed,
            policy=self.policy,
            limit=self.limit,
            remaining=max(0, self.limit - int(count)),
            reset_after=(float(newest) + self.window - now) if newest else 0.0,
            retry_after=max(0.0, retry_after),
        )

    def describe(self) -> Dict:
        return {"algorithm": self.policy, "limit": self.limit, "window_seconds": self.window}


class RedisFixedWindow(_RedisStrategy):
    policy = "fixed_window"
    script_source = FIXED_WINDOW_LUA

    def __init__(self, client, limit: int, window: float, clock: Clock = time.time):
        if limit <= 0 or window <= 0:
            raise ValueError("limit and window must be positive")
        super().__init__(client, clock)
        self.limit = limit
        self.window = window

    def _window_key(self, key: str, window_id: int) -> str:
        return f"{self._key(key)}:{window_id}"  # each window gets its own counter

    def _evaluate(self, key: str, cost: int, consume: bool) -> Decision:
        now = self._clock()
        window_id = int(now // self.window)
        reset_after = (window_id + 1) * self.window - now
        allowed, count = self._script(
            keys=[self._window_key(key, window_id)],
            args=[self.limit, cost, int(consume), int(reset_after * 1000) + 1000],
        )
        allowed = bool(allowed)
        return Decision(
            allowed=allowed,
            policy=self.policy,
            limit=self.limit,
            remaining=max(0, self.limit - int(count)),
            reset_after=reset_after,
            retry_after=0.0 if allowed else reset_after,
        )

    def reset(self, key: str) -> None:
        self._redis.delete(self._window_key(key, int(self._clock() // self.window)))

    def describe(self) -> Dict:
        return {"algorithm": self.policy, "limit": self.limit, "window_seconds": self.window}


class RedisLeakyBucket(_RedisStrategy):
    policy = "leaky_bucket"
    script_source = LEAKY_BUCKET_LUA

    def __init__(self, client, capacity: int, leak_rate: float, clock: Clock = time.time):
        if capacity <= 0 or leak_rate <= 0:
            raise ValueError("capacity and leak_rate must be positive")
        super().__init__(client, clock)
        self.capacity = capacity
        self.leak_rate = leak_rate

    def _evaluate(self, key: str, cost: int, consume: bool) -> Decision:
        allowed, before, level = self._script(
            keys=[self._key(key)],
            args=[self.capacity, self.leak_rate, self._clock(), cost, int(consume)],
        )
        allowed, before, level = bool(allowed), float(before), float(level)
        return Decision(
            allowed=allowed,
            policy=self.policy,
            limit=self.capacity,
            remaining=int(self.capacity - level),
            reset_after=level / self.leak_rate,
            retry_after=0.0 if allowed else (level + cost - self.capacity) / self.leak_rate,
            delay=before / self.leak_rate if allowed else 0.0,
        )

    def describe(self) -> Dict:
        return {"algorithm": self.policy, "queue_capacity": self.capacity, "dispatch_per_second": self.leak_rate}
