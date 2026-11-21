"""End-to-end demo: memory continuity from session A to session B, then a two-step forget.

Prerequisites: a running JarvisMind server and a DSH agent token (see setup_agent.py).

Environment:
  JARVISMIND_URL           server root, default http://127.0.0.1:8777
  JARVISMIND_AGENT_TOKEN   the DSH agent token (or JARVISMIND_AGENT_TOKEN_FILE)
  JARVISMIND_OWNER_TOKEN   optional. When set, the owner approves the forget request
                           automatically, the same way the console does. Without it the
                           demo waits (up to 180 s) for the owner to approve in the console.

Exit status is 0 only when every check passes.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
import uuid

REPO_SDK = pathlib.Path(__file__).resolve().parents[3] / "sdk" / "python"
sys.path.insert(0, str(REPO_SDK))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from dsh_hooks import build_system_prompt, submit_turn  # noqa: E402
from jarvismind_sdk import JarvisMindClient, JarvisMindError, NotFoundError, PermissionDeniedError  # noqa: E402

SAVED_STATES = {"SAVED", "UPDATED", "MERGED"}
BASE_PROMPT = "You are DSH, a helpful assistant."
APPROVAL_WAIT_SECONDS = 180


def _token() -> str:
    token = os.environ.get("JARVISMIND_AGENT_TOKEN", "").strip()
    if token:
        return token
    token_file = os.environ.get("JARVISMIND_AGENT_TOKEN_FILE", "")
    if not token_file:
        raise SystemExit("set JARVISMIND_AGENT_TOKEN or JARVISMIND_AGENT_TOKEN_FILE")
    return pathlib.Path(token_file).expanduser().read_text(encoding="utf-8").strip()


def _approve_as_owner(base_url: str, owner_token: str, request_id: str) -> dict:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/v1/forget-requests/{request_id}/approve",
        data=b"{}",
        method="POST",
    )
    request.add_header("Authorization", f"Bearer {owner_token}")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))["data"]
    except urllib.error.HTTPError as error:
        raise SystemExit(f"owner approval failed with HTTP {error.code}") from None


def _wait_for_decision(client: JarvisMindClient, request_id: str) -> str:
    deadline = time.monotonic() + APPROVAL_WAIT_SECONDS
    while time.monotonic() < deadline:
        for row in client.my_requests(page_size=50)["data"]:
            if row["id"] == request_id and row["status"] != "PENDING":
                return row["status"]
        time.sleep(2)
    return "PENDING"


def _check(results: list, label: str, condition: bool, detail: str = "") -> None:
    results.append(bool(condition))
    suffix = f" - {detail}" if detail else ""
    print(f"{'PASS' if condition else 'FAIL'} {label}{suffix}")


def main() -> int:
    base_url = os.environ.get("JARVISMIND_URL", "http://127.0.0.1:8777")
    client = JarvisMindClient(base_url, _token())
    owner_token = os.environ.get("JARVISMIND_OWNER_TOKEN", "").strip()
    token = uuid.uuid4().hex[:8]
    tea_phrase = f"茶叶{token}"
    results: list = []

    identity = client.me()
    print(f"agent: {identity['label']} (space {identity['active_space_id']}, permissions {', '.join(identity['permissions'])})")

    print("== Session A: the user states facts; the Post-Chat hook submits them ==")
    fact = client.remember("我最近在学习 Rust 异步编程", "dsh-session-a")
    tea = client.remember(f"我喜欢喝{tea_phrase}", "dsh-session-a")
    _check(
        results,
        "session A fact is stored or already known",
        fact["status"] in SAVED_STATES | {"DUPLICATE"},
        fact["status"],
    )
    _check(results, "session A preference is stored", tea["status"] in SAVED_STATES, tea["status"])
    tea_id = tea.get("memory_id")
    _check(results, "the preference has a memory id", bool(tea_id))

    print("== Session B: a new conversation; the Pre-Chat hook injects context ==")
    prompt_b = build_system_prompt(BASE_PROMPT, client, "我最近在学习什么？")
    _check(results, "session B recalls the Rust study", "Rust" in prompt_b)
    prompt_b_tea = build_system_prompt(BASE_PROMPT, client, f"我喜欢喝{tea_phrase}，你还记得吗？")
    _check(results, "session B recalls the tea preference", tea_phrase in prompt_b_tea)
    _check(
        results,
        "injected context is wrapped as untrusted data",
        'trust="untrusted-data"' in prompt_b,
    )

    print("== Two-step forget: step 1 registers the request, step 2 is the owner's decision ==")
    created = client.forget(tea_id, reason="用户要求遗忘这个偏好")
    _check(results, "step 1 registers a pending request", created["status"] == "PENDING", created["status"])
    _check(results, "step 1 never returns a confirmation token", "confirmation_token" not in created)
    still_there = client.get_memory(tea_id)
    _check(results, "the memory is unchanged until the owner decides", still_there["status"] == "ACTIVE")

    if owner_token:
        decided = _approve_as_owner(base_url, owner_token, created["id"])["status"]
        print("owner approved the request in the console-equivalent API call")
    else:
        print(f"waiting up to {APPROVAL_WAIT_SECONDS}s for the owner to approve in the console ...")
        decided = _wait_for_decision(client, created["id"])
    _check(results, "step 2 is executed by the owner", decided == "EXECUTED", decided)

    print("== Session C: the forgotten preference must not come back ==")
    prompt_c = build_system_prompt(BASE_PROMPT, client, f"我喜欢喝{tea_phrase}，你还记得吗？")
    _check(results, "session C does not recall the forgotten preference", token not in prompt_c)
    try:
        client.get_memory(tea_id)
        gone = False
    except NotFoundError:
        gone = True
    _check(results, "the forgotten memory is not readable through the agent API", gone)
    hits = client.search(tea_phrase, limit=20)["data"]
    _check(
        results,
        "search does not return the forgotten preference",
        all(token not in item["content"] for item in hits),
    )

    print("== Governance boundaries ==")
    phone = client.remember("记住我的手机号是 13800138000", "dsh-session-c")
    serialized = json.dumps(phone, ensure_ascii=False)
    _check(
        results,
        "a phone number is held as sensitive, not stored",
        phone["status"] == "PROTECTED" and phone["memory_id"] is None,
        phone["status"],
    )
    _check(results, "the phone number never appears in the response", "13800138000" not in serialized)
    try:
        client.update(tea_id, {"content": "我喝红茶"}, reason="更正")
        refused = False
    except PermissionDeniedError:
        refused = True
    _check(results, "an agent cannot request a correction (JMR non-human policy)", refused)

    failures = results.count(False)
    print(f"== {len(results) - failures}/{len(results)} checks passed ==")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except JarvisMindError as error:
        print(f"FAIL unexpected API error: {error}")
        sys.exit(1)
