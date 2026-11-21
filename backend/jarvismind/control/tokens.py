"""API token generation, digesting and the control-plane signing key.

A token is ``jmt_<public id>_<secret>``. The public id is random and is the only
part ever shown in listings; the secret is never stored. The database keeps
``HMAC-SHA256(control_key, token)``, so a copied control database cannot be used
to authenticate without the key file.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path
from typing import Tuple

TOKEN_PREFIX = "jmt_"
KEY_FILENAME = ".control_key"
_KEY_BYTES = 32


def generate_token() -> Tuple[str, str]:
    """Return ``(plaintext, public_prefix)``. The prefix never contains secret material."""
    public_id = secrets.token_hex(4)
    secret = secrets.token_urlsafe(32)
    return f"{TOKEN_PREFIX}{public_id}_{secret}", f"{TOKEN_PREFIX}{public_id}"


def digest_token(token: str, key: bytes) -> str:
    return hmac.new(key, token.encode("utf-8"), hashlib.sha256).hexdigest()


def digest_client(host: str, key: bytes) -> str:
    """Keyed digest of a client address, so access logs never store raw IPs."""
    return hmac.new(key, host.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def new_entity_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def _read_key(path: Path) -> bytes:
    raw = path.read_text(encoding="utf-8").strip()
    try:
        key = bytes.fromhex(raw)
    except ValueError as exc:
        raise RuntimeError(f"{KEY_FILENAME} is malformed; refusing to regenerate") from exc
    if len(key) != _KEY_BYTES:
        raise RuntimeError(f"{KEY_FILENAME} has the wrong length; refusing to regenerate")
    return key


def load_or_create_control_key(data_dir: Path) -> bytes:
    """Return the 32-byte control key, creating it once, exclusively, with owner-only rights.

    ``O_EXCL`` makes creation atomic, so two processes racing on a fresh install
    cannot each write a different key. An unreadable or malformed key fails
    closed: regenerating it would silently invalidate every issued token.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / KEY_FILENAME
    if path.exists():
        return _read_key(path)
    key = secrets.token_bytes(_KEY_BYTES)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _read_key(path)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(key.hex())
    return key
