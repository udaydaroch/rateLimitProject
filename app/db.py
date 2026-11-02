"""SQLite storage. Money is always stored as integer cents.

On Vercel the only writable directory is /tmp, which lives as long as the
serverless instance does. That makes the hosted version a resettable sandbox.
"""
import os
import sqlite3
import threading
from typing import Iterator, Set

DB_PATH = os.environ.get("BANK_DB_PATH") or ("/tmp/bank.db" if os.environ.get("VERCEL") else "bank.db")

_initialized: Set[str] = set()
_init_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    email       TEXT NOT NULL UNIQUE,
    plan        TEXT NOT NULL,
    created_at  REAL NOT NULL
);

-- Only a keyed hash of the secret is stored. `prefix` is a public lookup id.
CREATE TABLE IF NOT EXISTS api_keys (
    id            INTEGER PRIMARY KEY,
    customer_id   INTEGER NOT NULL REFERENCES customers(id),
    name          TEXT NOT NULL,
    prefix        TEXT NOT NULL UNIQUE,
    key_hash      TEXT NOT NULL,
    created_at    REAL NOT NULL,
    last_used_at  REAL,
    revoked_at    REAL
);

CREATE TABLE IF NOT EXISTS accounts (
    id             INTEGER PRIMARY KEY,
    customer_id    INTEGER NOT NULL REFERENCES customers(id),
    name           TEXT NOT NULL,
    currency       TEXT NOT NULL DEFAULT 'USD',
    balance_cents  INTEGER NOT NULL CHECK (balance_cents >= 0)
);

CREATE TABLE IF NOT EXISTS transactions (
    id            INTEGER PRIMARY KEY,
    account_id    INTEGER NOT NULL REFERENCES accounts(id),
    amount_cents  INTEGER NOT NULL,          -- negative = debit
    description   TEXT NOT NULL,
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tx_account ON transactions(account_id, id DESC);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    api_key_id      INTEGER NOT NULL,
    key             TEXT NOT NULL,
    request_hash    TEXT NOT NULL,
    status_code     INTEGER,                 -- NULL while the first request is in flight
    response_body   TEXT,
    created_at      REAL NOT NULL,
    PRIMARY KEY (api_key_id, key)
);

CREATE TABLE IF NOT EXISTS payouts (
    id             INTEGER PRIMARY KEY,
    account_id     INTEGER NOT NULL REFERENCES accounts(id),
    amount_cents   INTEGER NOT NULL,
    beneficiary    TEXT NOT NULL,
    status         TEXT NOT NULL,
    created_at     REAL NOT NULL,
    dispatch_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_events (
    id           INTEGER PRIMARY KEY,
    api_key_id   INTEGER NOT NULL,
    customer_id  INTEGER NOT NULL,
    endpoint     TEXT NOT NULL,
    status_code  INTEGER NOT NULL,
    billable     INTEGER NOT NULL,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_usage_customer ON usage_events(customer_id, created_at);
"""


def connect() -> sqlite3.Connection:
    # Create the schema lazily: serverless platforms don't reliably run startup hooks,
    # and a recycled instance starts with an empty /tmp.
    if DB_PATH not in _initialized:
        with _init_lock:
            if DB_PATH not in _initialized:
                init_db()
                _initialized.add(DB_PATH)
    return _open()


def _open() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = _open()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: one connection per request (autocommit mode)."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
