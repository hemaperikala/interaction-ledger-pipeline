"""PII protection: keyed hashing (HMAC-SHA256).

Phones are matched by HMAC(key, "phone:<workflow>|<+91...>"); the plaintext is never stored.
Other PII fields (customer name) are stored only as keyed digests (pseudonymised).
The key lives outside the database (env AU_PII_KEY, or a key file next to the DB).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path


class Hasher:
    def __init__(self, key: bytes):
        self._key = key

    def digest(self, namespace: str, value: str) -> str:
        msg = f"{namespace}\x00{value}".encode("utf-8")
        return hmac.new(self._key, msg, hashlib.sha256).hexdigest()

    def phone(self, workflow_id: str, canonical_phone: str) -> str:
        return self.digest(f"phone:{workflow_id}", canonical_phone)

    def key_check(self) -> str:
        return self.digest("key-check", "v1")


def load_key(db_path: str) -> bytes:
    env = os.environ.get("AU_PII_KEY")
    if env:
        return env.encode("utf-8")
    key_path = Path(str(db_path) + ".key")
    if key_path.exists():
        return bytes.fromhex(key_path.read_text().strip())
    key = secrets.token_bytes(32)
    key_path.write_text(key.hex())
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return key
