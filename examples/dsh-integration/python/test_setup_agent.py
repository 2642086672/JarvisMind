"""Regression tests for setup_agent.py's token-file path handling.

Run from this directory:

    python -m unittest -v test_setup_agent.py
"""
from __future__ import annotations

import os
import pathlib
import stat
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import setup_agent  # noqa: E402  (path setup must run first)

TOKEN = "jmt_plaintext_for_regression_test"
OWNER_SECRET = "owner-secret-for-regression-test"

# POSIX mode bits only exist on POSIX. On Windows, chmod() maps to the read-only
# attribute and stat() always reports 0o666/0o777, so asserting 0o600 there fails
# even though the script behaves correctly (NTFS ACLs protect the profile dir).
POSIX = os.name == "posix"


class SetupAgentTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name)
        self.owner_file = self.tmp / "owner.token"
        self.owner_file.write_text(OWNER_SECRET + "\n", encoding="utf-8")

    def _fake_owner_call(self, calls):
        def fake(base_url, owner_token, method, path, body=None):
            self.assertEqual(owner_token, OWNER_SECRET)
            calls.append((method, path, body))
            if path == "/api/v1/control/agents":
                return {"data": {"id": "agent_test"}}
            if path == "/api/v1/control/agents/agent_test/tokens":
                return {"data": {"token_prefix": "jmt_abc"}, "token": TOKEN}
            raise AssertionError(f"unexpected owner API path: {path}")

        return fake

    def _run_main(self, out: pathlib.Path):
        calls = []
        argv = [
            "setup_agent.py",
            "--owner-token-file",
            str(self.owner_file),
            "--out",
            str(out),
        ]
        with mock.patch.object(setup_agent, "_owner_call", side_effect=self._fake_owner_call(calls)), mock.patch.object(
            sys, "argv", argv
        ), mock.patch("builtins.print") as printed:
            rc = setup_agent.main()
        return rc, calls, printed

    def test_creates_missing_parent_directories_with_private_modes(self) -> None:
        out = self.tmp / "nested" / "deeper" / "dsh-agent.token"
        self.assertFalse(out.parent.exists())

        rc, calls, printed = self._run_main(out)

        self.assertEqual(rc, 0)
        self.assertEqual([call[1] for call in calls], [
            "/api/v1/control/agents",
            "/api/v1/control/agents/agent_test/tokens",
        ])
        self.assertEqual(out.read_text(encoding="utf-8"), TOKEN)
        self.assertTrue(out.parent.is_dir())
        self.assertTrue((self.tmp / "nested").is_dir())
        if POSIX:
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(out.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((self.tmp / "nested").stat().st_mode), 0o700)
        else:
            # The owner must still be able to write the token file.
            self.assertTrue(out.stat().st_mode & stat.S_IWRITE)

        # The plaintext token must never reach stdout.
        rendered = " ".join(str(call) for call in printed.call_args_list)
        self.assertNotIn(TOKEN, rendered)
        self.assertIn("jmt_abc", rendered)

    def test_existing_parent_keeps_its_mode(self) -> None:
        parent = self.tmp / "existing"
        parent.mkdir()
        parent.chmod(0o750)
        out = parent / "dsh-agent.token"

        rc, _calls, _printed = self._run_main(out)

        self.assertEqual(rc, 0)
        self.assertEqual(out.read_text(encoding="utf-8"), TOKEN)
        if POSIX:
            self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o750)
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        else:
            self.assertTrue(out.stat().st_mode & stat.S_IWRITE)

    def test_an_existing_wide_token_file_is_tightened(self) -> None:
        out = self.tmp / "dsh-agent.token"
        out.write_text("stale", encoding="utf-8")
        out.chmod(0o644)

        rc, _calls, _printed = self._run_main(out)

        self.assertEqual(rc, 0)
        self.assertEqual(out.read_text(encoding="utf-8"), TOKEN)
        if POSIX:
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        else:
            self.assertTrue(out.stat().st_mode & stat.S_IWRITE)


if __name__ == "__main__":
    unittest.main()