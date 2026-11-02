import os
import uuid

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app import db as app_db

# Optional: run the suite against real Postgres, each test in a throwaway schema.
# Use a direct (unpooled) URL, e.g. TEST_DATABASE_URL=$DATABASE_URL_UNPOOLED pytest
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(params=["memory", "redis"])
def client(request, tmp_path):
    """The whole API suite runs twice: in-memory limiters, then Redis limiters."""
    from app import policies
    from app.main import app

    schema = None
    if TEST_DATABASE_URL:
        import psycopg

        schema = f"test_{uuid.uuid4().hex[:12]}"
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
            conn.execute(f"CREATE SCHEMA {schema}")
        sep = "&" if "?" in TEST_DATABASE_URL else "?"
        app_db.configure(database_url=f"{TEST_DATABASE_URL}{sep}options=-csearch_path%3D{schema}")
    else:
        app_db.configure(path=str(tmp_path / "test.db"))

    policies.reset_limiters(request.param, fakeredis.FakeRedis() if request.param == "redis" else None)
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app_db.configure()
        if schema:
            with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
                conn.execute(f"DROP SCHEMA {schema} CASCADE")


def signup(client, plan="free"):
    r = client.post("/v1/signup", json={"name": "Acme", "email": f"{uuid.uuid4().hex}@ex.com", "plan": plan})
    assert r.status_code == 201
    key = r.json()["api_key"]
    headers = {"X-API-Key": key}
    accounts = client.get("/v1/accounts", headers=headers).json()["data"]
    return headers, accounts


def transfer(client, headers, src, dst, amount=100, key=None):
    return client.post("/v1/transfers", headers={**headers, "Idempotency-Key": key or uuid.uuid4().hex},
                       json={"from_account_id": src, "to_account_id": dst, "amount_cents": amount})


def test_api_key_is_only_stored_hashed(client):
    r = client.post("/v1/signup", json={"name": "A", "email": "a@ex.com"})
    key = r.json()["api_key"]
    secret = key.split("_")[3]
    with app_db.connection() as db:
        rows = db.execute("SELECT * FROM api_keys").fetchall()
    assert rows and all(secret not in str(dict(row)) for row in rows)


def test_invalid_and_revoked_keys_are_rejected(client):
    headers, _ = signup(client)
    assert client.get("/v1/accounts", headers={"X-API-Key": "bk_live_nope"}).status_code == 401

    key_id = client.get("/v1/keys", headers=headers).json()["data"][0]["id"]
    assert client.delete(f"/v1/keys/{key_id}", headers=headers).status_code == 200
    assert client.get("/v1/accounts", headers=headers).status_code == 401


def test_token_bucket_limits_api_burst_with_headers(client):
    headers, accounts = signup(client)  # free plan: burst 10 (signup's GET used one)
    statuses = [client.get(f"/v1/accounts/{accounts[0]['id']}/balance", headers=headers) for _ in range(12)]
    codes = [r.status_code for r in statuses]
    assert codes.count(200) == 9 and codes[-1] == 429

    limited = statuses[-1]
    assert limited.headers["RateLimit-Policy"] == "token_bucket"
    assert limited.headers["RateLimit-Remaining"] == "0"
    assert int(limited.headers["Retry-After"]) >= 1


def test_transfer_velocity_uses_sliding_window(client):
    headers, (checking, savings) = signup(client, plan="pro")
    codes = [transfer(client, headers, checking["id"], savings["id"]).status_code for _ in range(7)]
    assert codes == [201] * 5 + [429, 429]

    r = transfer(client, headers, checking["id"], savings["id"])
    assert r.json()["detail"]["policy"] == "sliding_window"


def test_idempotency_key_moves_money_once(client):
    headers, (checking, savings) = signup(client, plan="pro")
    key = uuid.uuid4().hex
    first = transfer(client, headers, checking["id"], savings["id"], amount=5000, key=key)
    second = transfer(client, headers, checking["id"], savings["id"], amount=5000, key=key)
    assert first.status_code == second.status_code == 201
    assert second.headers["Idempotent-Replayed"] == "true"
    assert first.json() == second.json()

    balance = client.get(f"/v1/accounts/{checking['id']}/balance", headers=headers).json()["balance_cents"]
    assert balance == checking["balance_cents"] - 5000

    reused = transfer(client, headers, checking["id"], savings["id"], amount=1, key=key)
    assert reused.status_code == 422


def test_replays_do_not_consume_transfer_velocity(client):
    headers, (checking, savings) = signup(client, plan="pro")
    key = uuid.uuid4().hex
    for _ in range(10):
        assert transfer(client, headers, checking["id"], savings["id"], key=key).status_code == 201
    assert transfer(client, headers, checking["id"], savings["id"]).status_code == 201


def test_rate_limited_transfer_can_be_retried_with_same_key(client):
    headers, (checking, savings) = signup(client, plan="pro")
    for _ in range(5):
        transfer(client, headers, checking["id"], savings["id"])
    key = uuid.uuid4().hex
    assert transfer(client, headers, checking["id"], savings["id"], key=key).status_code == 429

    from app import policies
    policies.limiters.transfer_velocity.reset(f"acct:{checking['id']}")
    assert transfer(client, headers, checking["id"], savings["id"], key=key).status_code == 201


def test_insufficient_funds_does_not_move_money(client):
    headers, (checking, savings) = signup(client, plan="pro")
    r = transfer(client, headers, checking["id"], savings["id"], amount=checking["balance_cents"] + 1)
    assert r.status_code == 422
    bal = client.get(f"/v1/accounts/{checking['id']}/balance", headers=headers).json()["balance_cents"]
    assert bal == checking["balance_cents"]


def test_payouts_are_shaped_by_leaky_bucket(client):
    headers, (checking, _) = signup(client, plan="pro")
    results = [client.post("/v1/payouts", headers={**headers, "Idempotency-Key": uuid.uuid4().hex},
                           json={"from_account_id": checking["id"], "amount_cents": 100, "beneficiary": f"E{i}"})
               for i in range(22)]
    accepted = [r for r in results if r.status_code == 202]
    assert len(accepted) == 20
    delays = [r.json()["queue_delay_seconds"] for r in accepted]
    assert delays == sorted(delays) and delays[0] < 0.1 and delays[-1] > 18
    assert results[-1].status_code == 429
    assert results[-1].json()["detail"]["policy"] == "leaky_bucket"


def test_cursor_pagination_walks_all_transactions(client):
    headers, (checking, _) = signup(client, plan="pro")
    seen, cursor = [], None
    while True:
        params = {"limit": 25, **({"cursor": cursor} if cursor else {})}
        page = client.get(f"/v1/accounts/{checking['id']}/transactions", headers=headers, params=params).json()
        seen += [t["id"] for t in page["data"]]
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
    assert len(seen) == 60 and len(set(seen)) == 60 and seen == sorted(seen, reverse=True)

    bad = client.get(f"/v1/accounts/{checking['id']}/transactions", headers=headers, params={"cursor": "!!"})
    assert bad.status_code == 400


def test_cannot_read_another_customers_account(client):
    headers_a, _ = signup(client)
    _, accounts_b = signup(client)
    r = client.get(f"/v1/accounts/{accounts_b[0]['id']}/balance", headers=headers_a)
    assert r.status_code == 404


def test_usage_is_metered(client):
    headers, accounts = signup(client, plan="pro")
    for _ in range(3):
        client.get(f"/v1/accounts/{accounts[0]['id']}/balance", headers=headers)
    usage = client.get("/v1/usage", headers=headers).json()
    endpoints = {e["endpoint"]: e["calls"] for e in usage["by_endpoint"]}
    assert endpoints["GET /v1/accounts/{account_id}/balance"] == 3
    assert endpoints["GET /v1/accounts"] == 1
    assert usage["today"]["quota_used"] == 4  # the usage call itself is not counted
    assert "GET /v1/usage" not in endpoints
    assert usage["limits"]["transfer_velocity"]["algorithm"] == "sliding_window"


def test_management_calls_work_when_api_bucket_is_empty(client):
    headers, _ = signup(client)
    while client.get("/v1/accounts", headers=headers).status_code != 429:
        pass
    assert client.get("/v1/usage", headers=headers).status_code == 200
    assert client.get("/v1/keys", headers=headers).status_code == 200


def test_payout_batch_is_one_api_call_but_shaped_per_item(client):
    headers, (checking, _) = signup(client)  # free plan: the batch must fit in one token
    batch = {"from_account_id": checking["id"],
             "payouts": [{"amount_cents": 100, "beneficiary": f"E{i}"} for i in range(25)]}
    r = client.post("/v1/payouts/batch", headers={**headers, "Idempotency-Key": uuid.uuid4().hex}, json=batch)
    assert r.status_code == 202
    body = r.json()
    assert (body["accepted"], body["rejected"]) == (20, 5)
    queued = [p["queue_delay_seconds"] for p in body["payouts"] if p["status"] == "queued"]
    assert queued == sorted(queued) and queued[-1] > 18
    assert {p["reason"] for p in body["payouts"] if p["status"] == "rejected"} == {"queue_full"}

    bal = client.get(f"/v1/accounts/{checking['id']}/balance", headers=headers).json()["balance_cents"]
    assert bal == checking["balance_cents"] - 20 * 100


def test_free_plan_daily_quota_is_enforced(client):
    from app import policies
    from app.ratelimit import FixedWindow

    headers, _ = signup(client)
    # Strategy pattern in action: shrink the quota by swapping the strategy.
    # (The swapped-in strategy is in-memory even on the Redis run; the app can't tell.)
    policies.limiters.daily_quota["free"].set_strategy(FixedWindow(limit=2, window=86_400))
    policies.limiters.api_traffic["free"].reset("key:1")
    assert client.get("/v1/accounts", headers=headers).status_code == 200
    assert client.get("/v1/accounts", headers=headers).status_code == 200
    r = client.get("/v1/accounts", headers=headers)
    assert r.status_code == 429
    assert r.json()["detail"]["error"] == "daily_quota_exceeded"


def test_auth_failures_lock_out_ip(client):
    for _ in range(20):
        assert client.get("/v1/accounts", headers={"X-API-Key": "bad"}).status_code == 401
    r = client.get("/v1/accounts", headers={"X-API-Key": "bad"})
    assert r.status_code == 429
    assert r.json()["detail"]["error"] == "too_many_auth_failures"
    assert "Retry-After" in r.headers


def test_limits_endpoint_shows_live_state_without_consuming(client):
    headers, (checking, savings) = signup(client, plan="pro")
    before = client.get("/v1/limits", headers=headers).json()
    assert client.get("/v1/limits", headers=headers).json()["api_traffic"]["remaining"] == \
        before["api_traffic"]["remaining"]

    transfer(client, headers, checking["id"], savings["id"])
    after = client.get("/v1/limits", headers=headers).json()
    velocity = {v["key"]: v["remaining"] for v in after["transfer_velocity"]}
    assert velocity[f"acct:{checking['id']}"] == 4
    assert velocity[f"acct:{savings['id']}"] == 5
    assert after["api_traffic"]["algorithm"] == "token_bucket"


def test_signups_are_limited_per_ip(client):
    for _ in range(10):
        signup(client)
    r = client.post("/v1/signup", json={"name": "A", "email": "x@ex.com"})
    assert r.status_code == 429
    assert r.headers["RateLimit-Policy"] == "fixed_window"


def test_client_ip_uses_forwarded_header_only_behind_trusted_proxy(monkeypatch):
    from starlette.requests import Request
    from app import deps

    req = Request({"type": "http", "headers": [(b"x-forwarded-for", b"203.0.113.9, 10.0.0.1")],
                   "client": ("10.0.0.1", 1234)})
    monkeypatch.setattr(deps, "TRUST_PROXY", False)
    assert deps.client_ip(req) == "10.0.0.1"  # don't let clients spoof their IP
    monkeypatch.setattr(deps, "TRUST_PROXY", True)
    assert deps.client_ip(req) == "203.0.113.9"


def test_duplicate_email_does_not_use_a_signup_slot(client):
    client.post("/v1/signup", json={"name": "A", "email": "dup@ex.com"})
    for _ in range(15):
        assert client.post("/v1/signup", json={"name": "A", "email": "dup@ex.com"}).status_code == 409
    assert client.post("/v1/signup", json={"name": "B", "email": "fresh@ex.com"}).status_code == 201


def test_dashboard_and_guide_pages_are_served(client):
    assert "Rate Limit Lab" in client.get("/dashboard").text
    guide = client.get("/guide")
    assert guide.status_code == 200 and guide.text.count('class="step"') == 17
