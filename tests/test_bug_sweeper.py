"""Standard-library tests for the scanner, ledger and snapshot helpers.

    python3 -m unittest discover -s tests -v
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")
FIXTURES = os.path.join(ROOT, "tests", "fixtures", "projects")


def load_scanner(env):
    """Import bin/scan-chats.py fresh, with its path constants read from `env`."""
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location("scan_chats", os.path.join(BIN, "scan-chats.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bug-sweeper-test-")
        self.state = os.path.join(self.tmp, "state")
        self.env = {
            "CLAUDE_PROJECTS_DIR": FIXTURES,
            "CODEX_HOME": os.path.join(self.tmp, "no-codex"),
            "BUG_SWEEPER_STATE_DIR": self.state,
            "LEDGER_FILE": os.path.join(self.state, "bug-sweeper.ledger.json"),
        }
        self.scanner = load_scanner(self.env)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_bin(self, *args, check=True):
        env = {**os.environ, **self.env}
        cmd = [sys.executable, *args] if args[0].endswith(".py") else list(args)
        return subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, check=check)


class ScoringTests(Base):
    def test_clear_complaint_clears_threshold(self):
        total, hits = self.scanner.score("the screen is black after I tap save, nothing happens")
        self.assertGreaterEqual(total, self.scanner.THRESHOLD)
        self.assertTrue(hits)

    def test_idioms_with_signal_words_are_cancelled(self):
        for text in ["break this all down into a checklist", "no problem, add error handling too",
                     "fix the spacing on the header"]:
            self.assertLess(self.scanner.score(text)[0], self.scanner.THRESHOLD, text)

    def test_weak_signal_alone_is_not_enough(self):
        self.assertLess(self.scanner.score("this looks weird")[0], self.scanner.THRESHOLD)

    def test_only_the_opening_is_scored(self):
        pasted = "here is the log for context\n" + ("x " * 300) + "crash error broken fails"
        self.assertLess(self.scanner.score(pasted)[0], self.scanner.THRESHOLD)


class ScanTests(Base):
    def findings(self):
        return self.scanner.scan(days=21, scan_all=True)

    def test_finds_typed_complaints_only(self):
        quotes = [f["quote"] for f in self.findings()]
        self.assertIn("the screen is black after I tap save, nothing happens", quotes)
        self.assertIn("the login form won't submit on mobile", quotes)
        # Tool results carry no promptSource and are never treated as the user speaking.
        self.assertFalse(any(q.startswith("Tool result") for q in quotes))
        self.assertFalse(any("break this all down" in q for q in quotes))

    def test_headless_sdk_cli_sessions_are_excluded(self):
        projects = {f["project"] for f in self.findings()}
        self.assertNotIn("/home/dev/loops", projects)

    def test_repeated_and_resolved_flags(self):
        by_quote = {f["quote"]: f for f in self.findings()}
        black = by_quote["the screen is black after I tap save, nothing happens"]
        self.assertTrue(black["repeatedLater"])
        self.assertFalse(black["likelyResolvedInSession"])
        login = by_quote["the login form won't submit on mobile"]
        self.assertTrue(login["likelyResolvedInSession"])
        self.assertFalse(login["repeatedLater"])

    def test_lookback_window_excludes_old_findings(self):
        self.assertEqual(self.scanner.scan(days=21, scan_all=False), [])

    def test_ids_are_stable(self):
        self.assertEqual([f["id"] for f in self.findings()], [f["id"] for f in self.findings()])


class LedgerTests(Base):
    def test_marked_findings_never_come_back(self):
        first = self.scanner.scan(days=21, scan_all=True)
        target = first[0]["id"]
        out = self.run_bin(os.path.join(BIN, "ledger.py"), "mark", target, "fixed", "null check in save handler")
        self.assertIn("-> fixed", out.stdout)
        again = [f["id"] for f in self.scanner.scan(days=21, scan_all=True)]
        self.assertNotIn(target, again)
        self.assertEqual(len(again), len(first) - 1)
        got = json.loads(self.run_bin(os.path.join(BIN, "ledger.py"), "get", target).stdout)
        self.assertEqual(got["status"], "fixed")

    def test_rejects_unknown_status(self):
        out = self.run_bin(os.path.join(BIN, "ledger.py"), "mark", "abc", "done", check=False)
        self.assertEqual(out.returncode, 2)
        self.assertFalse(os.path.exists(self.env["LEDGER_FILE"]))

    def test_list_filters_by_status(self):
        ledger = os.path.join(BIN, "ledger.py")
        self.run_bin(ledger, "mark", "a1", "fixed", "one")
        self.run_bin(ledger, "mark", "b2", "needs-user", "two")
        out = self.run_bin(ledger, "list", "needs-user").stdout
        self.assertIn("b2", out)
        self.assertNotIn("a1", out)


class SnapshotTests(Base):
    def test_save_then_restore_puts_exact_files_back(self):
        project = os.path.join(self.tmp, "project")
        os.makedirs(project)
        edited = os.path.join(project, "app.js")
        untouched = os.path.join(project, "notes.txt")
        with open(edited, "w") as fh:
            fh.write("original\n")
        with open(untouched, "w") as fh:
            fh.write("user work\n")

        snap = self.run_bin("bash", os.path.join(BIN, "snapshot.sh"), "save", edited).stdout.strip()
        self.assertTrue(snap.startswith(os.path.join(self.state, "bug-sweeper.snapshots")))

        with open(edited, "w") as fh:
            fh.write("broken fix\n")
        with open(untouched, "w") as fh:
            fh.write("user kept working\n")

        out = self.run_bin("bash", os.path.join(BIN, "snapshot.sh"), "restore", snap).stdout
        self.assertIn("1 file(s) restored", out)
        with open(edited) as fh:
            self.assertEqual(fh.read(), "original\n")
        # Restore touches only snapshotted files, never the user's other edits.
        with open(untouched) as fh:
            self.assertEqual(fh.read(), "user kept working\n")

    def test_save_refuses_missing_file(self):
        out = self.run_bin("bash", os.path.join(BIN, "snapshot.sh"), "save",
                           os.path.join(self.tmp, "nope.js"), check=False)
        self.assertEqual(out.returncode, 3)


if __name__ == "__main__":
    unittest.main()
