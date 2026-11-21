"""P0-1 fix: a SERVER-ONLY signing key, physically separated from api_token.

Why this file exists
--------------------
The previous implementation derived both TrustedPrincipal.mac and
SignedGrant.signature from ``Settings.api_token`` with a constant HMAC context.
That token is the SAME shared secret the client sends in ``X-API-Token``
(``core/security.py:token_matches``). A domain-separation label does not make a
client-known secret server-only: anyone holding the token could recompute the
MAC offline and mint an arbitrary principal. Independent review P0-1 reproduced
this with a live test.

New rules
---------
* The signing key is never derived from ``api_token`` or any client-visible
  value. It comes from ``JARVISMIND_P1_GRANT_KEY`` (hex, >= 32 bytes) or, when that is
  unset, from a 0600 file ``<data_dir>/.p1_grant_key`` generated once with
  ``secrets.token_bytes(32)``.
* The key file is written atomically (temp file + os.replace) and never logged,
  returned by an API, or persisted to SQLite.
* A missing/corrupt/short key is FAIL-CLOSED (``GrantKeyError``). There is no
  fallback to an unkeyed digest, a random in-memory key, or the api token.
* ``derive_subkey()`` still applies domain separation, but on top of a secret
  the client does not have. That is what makes the separation meaningful.

Rotation: ``key_version`` in SignedGrant invalidates old grants. To rotate,
replace the env value / key file and bump the configured key version; all
previously issued grants then fail verification. Complexity: O(1) per load,
one file read per process (callers may cache the returned bytes).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path
from typing import Any

from ..core.errors import ForbiddenError

KEY_ENV = "JARVISMIND_P1_GRANT_KEY"
KEY_FILENAME = ".p1_grant_key"
MIN_KEY_BYTES = 32
GENERATED_KEY_BYTES = 32

_PRINCIPAL_CONTEXT = b"jme/p1-trusted-principal/v1"
_GRANT_CONTEXT = b"jme/p1-signed-grant/v1"


class GrantKeyError(ForbiddenError):
    """Server signing key missing/invalid; fail closed, never fall back."""

    code = "grant_key_unavailable"
    message = "Server signing key is unavailable"


def _decode_hex(raw: str) -> bytes:
    try:
        value = bytes.fromhex(raw.strip())
    except (ValueError, TypeError) as exc:
        raise GrantKeyError("signing key must be hex-encoded") from exc
    if len(value) < MIN_KEY_BYTES:
        raise GrantKeyError(
            f"signing key must be >= {MIN_KEY_BYTES} bytes, got {len(value)}"
        )
    return value


def load_grant_key(settings: Any) -> bytes:
    """Return the server-only key. Environment wins over the key file."""
    configured = (os.environ.get(KEY_ENV) or "").strip()
    if configured:
        return _decode_hex(configured)

    raw_dir = getattr(settings, "data_dir", None)
    if raw_dir is None or not str(raw_dir):
        # Fail closed instead of silently creating a key file in the current
        # working directory. A stub settings object without a data_dir and no
        # JARVISMIND_P1_GRANT_KEY must not be able to derive a fingerprint.
        raise GrantKeyError(
            "no server key configured: set JARVISMIND_P1_GRANT_KEY or provide data_dir"
        )
    data_dir = Path(raw_dir)
    path = data_dir / KEY_FILENAME
    if path.is_file():
        return _decode_hex(path.read_text(encoding="utf-8"))

    # First run: generate a key that the client never sees. Atomic replace so a
    # concurrent reader cannot observe a half-written key file.
    raw = secrets.token_bytes(GENERATED_KEY_BYTES)
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(raw.hex(), encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Windows has no POSIX permission bits; the private data dir still applies.
        pass
    return raw


def derive_subkey(key: bytes, context: bytes) -> bytes:
    """Domain-separated subkey. ``key`` is server-only, unlike api_token."""
    return hmac.new(key, context, hashlib.sha256).digest()


def subkey_for(settings: Any, context: bytes) -> bytes:
    return derive_subkey(load_grant_key(settings), context)


def principal_context() -> bytes:
    return _PRINCIPAL_CONTEXT


def grant_context() -> bytes:
    return _GRANT_CONTEXT


def key_source(settings: Any) -> str:
    """Non-secret provenance label for audit/status output."""
    return "env" if (os.environ.get(KEY_ENV) or "").strip() else KEY_FILENAME
