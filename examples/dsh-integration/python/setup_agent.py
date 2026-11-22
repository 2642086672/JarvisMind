"""Create the DSH agent and its token through the owner API (the console does the same).

Usage:
    python setup_agent.py --owner-token-file ~/.jarvismind/owner.token --out ~/.jarvismind/dsh-agent.token

The agent token is written to --out with owner-only permissions and is never printed.
Missing parent directories are created with mode 0700, so a fresh install can write
``~/.jarvismind/dsh-agent.token`` directly. Run this once per installation: each run
creates a new agent.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

# Least privilege for a chat application: read, search and context, plus governed writes
# (remember) and governed forget requests. Sensitive access and graph access are not granted.
AGENT_PERMISSIONS = ["read", "search", "context", "write", "delete"]
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def _ensure_private_parent(target: pathlib.Path) -> None:
    """Create the token's parent directories, keeping newly created ones private.

    ``os.open`` cannot create missing parents, so a fresh install with
    ``--out ~/.jarvismind/dsh-agent.token`` used to fail with ENOENT. Every
    directory this helper creates is chmodded to 0700 (mkdir's mode is masked
    by the process umask); pre-existing directories are left untouched.
    """
    parent = target.parent
    missing = []
    probe = parent
    while not probe.exists() and probe != probe.parent:
        missing.append(probe)
        probe = probe.parent

    parent.mkdir(parents=True, exist_ok=True)
    for directory in missing:
        try:
            os.chmod(directory, PRIVATE_DIR_MODE)
        except OSError:
            # "Best effort": a filesystem without POSIX modes must not block setup.
            pass


def _owner_call(base_url: str, owner_token: str, method: str, path: str, body=None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(base_url.rstrip("/") + path, data=data, method=method)
    request.add_header("Authorization", f"Bearer {owner_token}")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise SystemExit(f"owner API returned HTTP {error.code}: {detail[:300]}") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default=os.environ.get("JARVISMIND_URL", "http://127.0.0.1:8777"))
    parser.add_argument("--owner-token-file", required=True, help="file that contains the owner token")
    parser.add_argument("--name", default="DSH", help="display name of the agent")
    parser.add_argument("--out", required=True, help="where to write the agent token (mode 0600)")
    parser.add_argument("--expires-in-days", type=int, default=90)
    args = parser.parse_args()

    owner_token = pathlib.Path(args.owner_token_file).expanduser().read_text(encoding="utf-8").strip()
    agent = _owner_call(
        args.base_url,
        owner_token,
        "POST",
        "/api/v1/control/agents",
        {
            "name": args.name,
            "agent_type": "dsh",
            "description": "DSH chat application",
            "permissions": AGENT_PERMISSIONS,
        },
    )["data"]
    issued = _owner_call(
        args.base_url,
        owner_token,
        "POST",
        f"/api/v1/control/agents/{agent['id']}/tokens",
        {"name": "dsh-primary", "expires_in_days": args.expires_in_days},
    )

    out = pathlib.Path(args.out).expanduser()
    _ensure_private_parent(out)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE_MODE)
    try:
        os.fchmod(fd, PRIVATE_FILE_MODE)
    except OSError:
        pass
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(issued["token"])
    print(f"agent {agent['id']} created with permissions: {', '.join(AGENT_PERMISSIONS)}")
    print(f"token written to {out} (mode 0600); token prefix {issued['data']['token_prefix']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
