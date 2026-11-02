"""API key generation and verification.

Key format:  bk_live_<prefix>_<secret>
  * prefix: 8 random chars, stored in plaintext, used to find the row (indexed).
  * secret: 32 random chars, never stored. We keep HMAC-SHA256(pepper, secret).

Why not bcrypt? API keys are long and random (~190 bits), so they can't be
brute-forced the way passwords can; a fast keyed hash is enough and keeps
per-request auth cheap. The pepper lives outside the database, so a leaked DB
dump alone can't be used to verify guesses.
"""
import hashlib
import hmac
import os
import secrets
import string
from typing import Optional, Tuple

PEPPER = os.environ.get("API_KEY_PEPPER", "dev-only-pepper-change-me").encode()
_ALPHABET = string.ascii_letters + string.digits


def _random(n: int) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


def hash_secret(secret: str) -> str:
    return hmac.new(PEPPER, secret.encode(), hashlib.sha256).hexdigest()


def generate_api_key() -> Tuple[str, str, str]:
    """Returns (full_key_to_show_once, prefix, key_hash)."""
    prefix, secret = _random(8), _random(32)
    return f"bk_live_{prefix}_{secret}", prefix, hash_secret(secret)


def parse_api_key(raw: str) -> Optional[Tuple[str, str]]:
    parts = raw.split("_")
    if len(parts) != 4 or parts[0] != "bk" or parts[1] != "live":
        return None
    prefix, secret = parts[2], parts[3]
    if len(prefix) != 8 or len(secret) != 32:
        return None
    return prefix, secret


def verify_secret(secret: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_secret(secret), stored_hash)
