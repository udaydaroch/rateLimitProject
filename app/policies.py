"""Which algorithm protects which part of the bank, and with what numbers.

| Concern                         | Algorithm        | Keyed by        |
|---------------------------------|------------------|-----------------|
| General API traffic (bursty)    | Token bucket     | API key         |
| Daily quota / billing           | Fixed window     | customer        |
| Transfer velocity (anti-fraud)  | Sliding window   | source account  |
| Payouts to the payment rail     | Leaky bucket     | customer        |
| Bad API key attempts            | Fixed window     | client IP       |
| Sandbox signups                 | Fixed window     | client IP       |

Because each limiter is a RateLimiter context wrapping a strategy, changing an
algorithm is a config edit below, e.g. switching auth lockout to
("sliding_window", {"limit": 20, "window": 60}) needs no other code change.
"""
from dataclasses import dataclass
from typing import Dict, Tuple

from .ratelimit import RateLimiter, create_strategy

DAY = 24 * 60 * 60

StrategyConfig = Tuple[str, Dict]


@dataclass(frozen=True)
class Plan:
    name: str
    daily_quota: int
    burst: int                  # token bucket capacity
    refill_per_sec: float       # token bucket refill rate
    allow_overage: bool         # pro plans keep working past quota and are billed for it
    price_per_1k_cents: int     # usage price per 1,000 billable calls
    overage_per_1k_cents: int


PLANS: Dict[str, Plan] = {
    "free": Plan("free", daily_quota=1_000, burst=10, refill_per_sec=2,
                 allow_overage=False, price_per_1k_cents=0, overage_per_1k_cents=0),
    "pro": Plan("pro", daily_quota=50_000, burst=50, refill_per_sec=20,
                allow_overage=True, price_per_1k_cents=40, overage_per_1k_cents=80),
}

SHARED_POLICIES: Dict[str, StrategyConfig] = {
    # Max 5 outgoing transfers from one account in any rolling 60 seconds.
    "transfer_velocity": ("sliding_window", {"limit": 5, "window": 60}),
    # Payment rail takes 1 payout/sec per customer; queue at most 20.
    "payout_rail": ("leaky_bucket", {"capacity": 20, "leak_rate": 1.0}),
    # 20 failed auth attempts per IP per minute, then locked out until the minute rolls.
    "auth_failures": ("fixed_window", {"limit": 20, "window": 60}),
    # Public sandbox: 10 new customers per IP per hour.
    "signups": ("fixed_window", {"limit": 10, "window": 3600}),
}


def plan_policies(plan: Plan) -> Dict[str, StrategyConfig]:
    return {
        "api_traffic": ("token_bucket", {"capacity": plan.burst, "refill_rate": plan.refill_per_sec}),
        "daily_quota": ("fixed_window", {"limit": plan.daily_quota, "window": DAY}),
    }


def _build(name: str, config: StrategyConfig) -> RateLimiter:
    algorithm, params = config
    return RateLimiter(name, create_strategy(algorithm, **params))


class Limiters:
    """All limiter instances for the process. Recreate to wipe state (tests)."""

    def __init__(self) -> None:
        per_plan = {name: plan_policies(plan) for name, plan in PLANS.items()}
        self.api_traffic: Dict[str, RateLimiter] = {
            name: _build(f"api_traffic.{name}", cfg["api_traffic"]) for name, cfg in per_plan.items()
        }
        self.daily_quota: Dict[str, RateLimiter] = {
            name: _build(f"daily_quota.{name}", cfg["daily_quota"]) for name, cfg in per_plan.items()
        }
        self.transfer_velocity = _build("transfer_velocity", SHARED_POLICIES["transfer_velocity"])
        self.payout_rail = _build("payout_rail", SHARED_POLICIES["payout_rail"])
        self.auth_failures = _build("auth_failures", SHARED_POLICIES["auth_failures"])
        self.signups = _build("signups", SHARED_POLICIES["signups"])


limiters = Limiters()


def reset_limiters() -> None:
    global limiters
    limiters = Limiters()
