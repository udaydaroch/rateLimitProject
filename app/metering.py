"""Usage reporting and billing estimates built from `usage_events`."""
import datetime as dt
from typing import Dict

from . import policies
from .db import Database
from .policies import Plan


def _utc_day_start(now: dt.datetime) -> dt.datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def usage_report(db: Database, customer_id: int, plan: Plan, days: int = 14) -> Dict:
    now = dt.datetime.now(dt.timezone.utc)
    today = _utc_day_start(now)
    since = today - dt.timedelta(days=days - 1)
    month_start = today.replace(day=1)

    daily_rows = db.execute(
        "SELECT day, COUNT(*) AS calls, SUM(billable) AS billable, "
        "SUM(CASE WHEN status_code = 429 THEN 1 ELSE 0 END) AS rate_limited "
        "FROM usage_events WHERE customer_id = ? AND created_at >= ? GROUP BY day ORDER BY day",
        (customer_id, min(since, month_start).timestamp()),
    ).fetchall()
    by_day = {r["day"]: r for r in daily_rows}

    daily = []
    for i in range(days):
        day = (since + dt.timedelta(days=i)).date().isoformat()
        r = by_day.get(day)
        daily.append({
            "date": day,
            "calls": r["calls"] if r else 0,
            "billable": r["billable"] if r else 0,
            "rate_limited": r["rate_limited"] if r else 0,
        })

    endpoint_rows = db.execute(
        "SELECT endpoint, COUNT(*) AS calls, SUM(CASE WHEN status_code = 429 THEN 1 ELSE 0 END) AS rate_limited "
        "FROM usage_events WHERE customer_id = ? AND created_at >= ? GROUP BY endpoint ORDER BY calls DESC",
        (customer_id, month_start.timestamp()),
    ).fetchall()

    # Billing: quota calls at the base price, anything above the daily quota at the overage price.
    month_days = [r for d, r in by_day.items() if d >= month_start.date().isoformat()]
    billable = sum(r["billable"] for r in month_days)
    overage = sum(max(0, r["billable"] - plan.daily_quota) for r in month_days) if plan.allow_overage else 0
    included = billable - overage
    estimated_cents = round(included * plan.price_per_1k_cents / 1000 + overage * plan.overage_per_1k_cents / 1000)

    quota = policies.limiters.daily_quota[plan.name].peek(f"cust:{customer_id}")
    quota_used = plan.daily_quota - quota.remaining
    return {
        "plan": plan.name,
        "today": {
            "quota": plan.daily_quota,
            "quota_used": quota_used,
            "quota_remaining": max(0, plan.daily_quota - quota_used),
            "resets_at": (today + dt.timedelta(days=1)).isoformat(),
        },
        "limits": {
            "api_traffic": policies.limiters.api_traffic[plan.name].describe(),
            "daily_quota": policies.limiters.daily_quota[plan.name].describe(),
            "transfer_velocity": policies.limiters.transfer_velocity.describe(),
            "payout_rail": policies.limiters.payout_rail.describe(),
        },
        "daily": daily,
        "by_endpoint": [dict(r) for r in endpoint_rows],
        "billing": {
            "period": month_start.strftime("%Y-%m"),
            "billable_calls": billable,
            "overage_calls": overage,
            "estimated_cents": estimated_cents,
        },
    }
