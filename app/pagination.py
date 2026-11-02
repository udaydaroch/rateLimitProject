"""Opaque cursor (keyset) pagination.

Offset pagination (`?page=40`) breaks on a ledger that is constantly being
written to: new transactions shift every page, so clients skip or duplicate
rows, and `OFFSET 40000` gets slower the deeper you go. A cursor instead says
"give me rows older than id X", which is stable and uses the index.
"""
import base64
import json
from typing import Optional

from fastapi import HTTPException


def encode_cursor(last_id: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"id": last_id}).encode()).decode().rstrip("=")


def decode_cursor(cursor: Optional[str]) -> Optional[int]:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded))["id"]
        if not isinstance(value, int):
            raise ValueError
        return value
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, {"error": "invalid_cursor"})
