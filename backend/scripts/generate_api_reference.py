"""Generate docs/API-REFERENCE.md from a running server's OpenAPI document.

Usage:
    python scripts/generate_api_reference.py --base-url http://127.0.0.1:8777 --out ../docs/API-REFERENCE.md

The script uses only the standard library. It lists every operation under /api/v1 and
/health, grouped by its first tag. Authentication rules are documented in docs/API.md.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import urllib.request
from typing import Dict, List, Tuple

METHODS = ("get", "post", "patch", "put", "delete")


def collect(spec: dict) -> Tuple[Dict[str, List[Tuple[str, str, str]]], int]:
    groups: Dict[str, List[Tuple[str, str, str]]] = {}
    count = 0
    for path in sorted(spec.get("paths", {})):
        if not (path.startswith("/api/v1") or path == "/health"):
            continue
        for method, operation in spec["paths"][path].items():
            if method not in METHODS:
                continue
            tags = operation.get("tags") or ["untagged"]
            summary = operation.get("summary") or operation.get("operationId") or ""
            groups.setdefault(tags[0], []).append((method.upper(), path, summary))
            count += 1
    return groups, count


def render(spec: dict, groups: Dict[str, List[Tuple[str, str, str]]], count: int) -> str:
    version = spec.get("info", {}).get("version", "unknown")
    lines = [
        "# API reference (generated)",
        "",
        f"Generated from the OpenAPI document served by JarvisMind {version} (`GET /openapi.json`).",
        "It lists every route the server exposes under `/api/v1` plus `/health`.",
        "The authentication rules, permissions and examples are in [API.md](API.md).",
        "",
        f"Total operations: **{count}**.",
        "",
    ]
    for tag in sorted(groups):
        lines.append(f"## {tag}")
        lines.append("")
        lines.append("| Method | Path | Summary |")
        lines.append("| --- | --- | --- |")
        for method, path, summary in groups[tag]:
            lines.append(f"| `{method}` | `{path}` | {summary} |")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:8777")
    parser.add_argument("--out", required=True, help="file to write, for example ../docs/API-REFERENCE.md")
    args = parser.parse_args()

    url = args.base_url.rstrip("/") + "/openapi.json"
    with urllib.request.urlopen(url, timeout=30) as response:
        spec = json.loads(response.read().decode("utf-8"))
    groups, count = collect(spec)
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(spec, groups, count), encoding="utf-8")
    print(f"wrote {out} with {count} operations across {len(groups)} groups")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
