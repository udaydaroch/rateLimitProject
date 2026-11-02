"""Idempotency keys for money-moving endpoints.

A client sends `Idempotency-Key: <uuid>` with a transfer. If the network drops
and the client retries, the second request must NOT move money again; it gets
the stored response of the first one.

  * Same key + same body, finished      -> replay stored response
  * Same key + same body, still running -> 409 (don't run it twice concurrently)
  * Same key + different body           -> 422 (client bug)
  * 429 / 5xx outcomes are not stored, so the client can retry with the same key.

The handler's database writes and the stored response are committed in ONE
transaction, so a crash can never leave "money moved but key not completed".
"""
import hashlib
import json
import sqlite3
import time
from typing import Any, Callable, Optional, Tuple

from fastapi import HTTPException
from fastapi.responses import JSONResponse

TTL_SECONDS = 24 * 60 * 60
STALE_IN_FLIGHT_SECONDS = 60  # an in-flight marker this old belongs to a crashed request


def request_fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _begin(db: sqlite3.Connection, api_key_id: int, key: str, fingerprint: str) -> Optional[Tuple[int, Any]]:
    now = time.time()
    db.execute(
        "DELETE FROM idempotency_keys WHERE api_key_id = ? AND key = ? "
        "AND (created_at < ? OR (status_code IS NULL AND created_at < ?))",
        (api_key_id, key, now - TTL_SECONDS, now - STALE_IN_FLIGHT_SECONDS),
    )
    try:
        db.execute(
            "INSERT INTO idempotency_keys (api_key_id, key, request_hash, created_at) VALUES (?, ?, ?, ?)",
            (api_key_id, key, fingerprint, now),
        )
        return None
    except sqlite3.IntegrityError:
        row = db.execute("SELECT request_hash, status_code, response_body FROM idempotency_keys "
                         "WHERE api_key_id = ? AND key = ?", (api_key_id, key)).fetchone()
    if row["request_hash"] != fingerprint:
        raise HTTPException(422, {"error": "idempotency_key_reused",
                                  "message": "This Idempotency-Key was used with a different request body."})
    if row["status_code"] is None:
        raise HTTPException(409, {"error": "request_in_progress",
                                  "message": "A request with this Idempotency-Key is still being processed."})
    return row["status_code"], json.loads(row["response_body"])


def _complete(db: sqlite3.Connection, api_key_id: int, key: str, status: int, body: Any) -> None:
    db.execute("UPDATE idempotency_keys SET status_code = ?, response_body = ? WHERE api_key_id = ? AND key = ?",
               (status, json.dumps(body), api_key_id, key))


def _abandon(db: sqlite3.Connection, api_key_id: int, key: str) -> None:
    db.execute("DELETE FROM idempotency_keys WHERE api_key_id = ? AND key = ?", (api_key_id, key))


def run_idempotent(
    db: sqlite3.Connection,
    api_key_id: int,
    key: str,
    payload: dict,
    status_code: int,
    handler: Callable[[], dict],
) -> JSONResponse:
    if not key or len(key) > 255:
        raise HTTPException(400, {"error": "idempotency_key_required",
                                  "message": "Send a unique Idempotency-Key header (max 255 chars)."})

    replay = _begin(db, api_key_id, key, request_fingerprint(payload))
    if replay is not None:
        status, body = replay
        return JSONResponse(status_code=status, content=body, headers={"Idempotent-Replayed": "true"})

    try:
        db.execute("BEGIN IMMEDIATE")
        body = handler()
        _complete(db, api_key_id, key, status_code, body)
        db.execute("COMMIT")
    except HTTPException as exc:
        db.execute("ROLLBACK")
        if exc.status_code == 429 or exc.status_code >= 500:
            _abandon(db, api_key_id, key)
        else:
            _complete(db, api_key_id, key, exc.status_code, {"detail": exc.detail})
        raise
    except Exception:
        db.execute("ROLLBACK")
        _abandon(db, api_key_id, key)
        raise

    return JSONResponse(status_code=status_code, content=body)
