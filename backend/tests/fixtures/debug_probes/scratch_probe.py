"""Temporary API response-shape probe (P0-03A-R4 hardened).

Originally this module read the production token file and called the live
production API at import time.  It is now an explicit CLI tool:

* no module-level side effects (no token file read, no network call);
* token must be supplied as an explicit value (``--token`` or env), never a
  path; the production token file is never opened;
* URL must be supplied explicitly; the request is built only through
  ``app.core.production_guard.build_debug_request``, which refuses the
  production service port and requires an explicit allow switch;
* the network call happens in ``main()`` only, never on import.

Running it with no arguments fails closed with exit code 2.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jarvismind.core.production_guard import (  # noqa: E402
    ENV_DEBUG_API_ALLOW,
    ProductionAccessBlocked,
    build_debug_request,
)


def probe(url: str, token: str, query: str, timeout: float) -> None:
    """Send one explicit request and print the response shape."""
    q = urllib.parse.quote(query)
    separator = "&" if "?" in url else "?"
    full = url + separator + "q=" + q + "&page=1&page_size=2"
    try:
        request = build_debug_request(full, token)
    except ProductionAccessBlocked as exc:
        print("[P0-03A-R4 REFUSED]", exc)
        return
    response = json.load(urllib.request.urlopen(request, timeout=timeout))
    print("META:", json.dumps(response["meta"], ensure_ascii=False))
    data = response.get("data") or []
    if data:
        print("FIRST RECORD KEYS:", list(data[0].keys()))
        print(json.dumps(data[0], ensure_ascii=False, indent=2))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Explicit debug-only API response-shape probe"
    )
    parser.add_argument("--url", required=True, help="显式调试 API 地址")
    parser.add_argument("--token", default=None, help="显式 token 值（非文件路径）")
    parser.add_argument("--query", default="猫")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args(argv)

    token = args.token or os.environ.get("JARVISMIND_DEBUG_TOKEN", "")
    try:
        # Validate before any network I/O so a bad target never sends bytes.
        build_debug_request(args.url, token)
    except ProductionAccessBlocked as exc:
        print("[P0-03A-R4 REFUSED]", exc)
        return 2
    if os.environ.get(ENV_DEBUG_API_ALLOW, "") != "1":
        print("[P0-03A-R4 REFUSED] 需要显式开启调试 API 许可")
        return 2
    probe(args.url, token, args.query, args.timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
