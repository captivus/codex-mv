#!/usr/bin/env python3
"""Tests for codex-mv.

Every test builds a throwaway CODEX_HOME under a temp directory. Nothing here
reads or writes the user's real ~/.codex.

Two layers:
  * structural tests  - assert the files and DB rows codex-mv is supposed to change
  * behavioural tests - drive the real `codex app-server` and assert that the
                        renamed project's sessions actually come back from
                        thread/list, which is what `codex resume` filters on

The behavioural tests are skipped automatically when the codex CLI is absent.
"""

import importlib.machinery
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CODEX_MV = os.path.join(os.path.dirname(HERE), "codex-mv")

# Mirrors the columns codex-mv touches. The real table has many more, all of
# which are irrelevant here; the behavioural tests exercise the real schema.
THREADS_SCHEMA = """
CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    rollout_path TEXT NOT NULL,
    created_at INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'cli',
    model_provider TEXT NOT NULL DEFAULT 'openai',
    cwd TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    sandbox_policy TEXT NOT NULL DEFAULT '{"type":"workspace-write"}',
    approval_mode TEXT NOT NULL DEFAULT 'on-request',
    preview TEXT NOT NULL DEFAULT 'hello'
);
"""


_MODULE = None


def codex_mv_module():
    """Import the tool as a module so helpers can be unit-tested directly.

    It has no .py extension, being a CLI, so load it by path.
    """
    global _MODULE
    if _MODULE is None:
        import importlib.util
        spec = importlib.util.spec_from_loader(
            "codex_mv",
            importlib.machinery.SourceFileLoader("codex_mv", CODEX_MV))
        _MODULE = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_MODULE)
    return _MODULE


def run_codex_mv(*args):
    return subprocess.run(
        [sys.executable, CODEX_MV, *args],
        capture_output=True, text=True,
    )


FIXTURE_META = os.path.join(HERE, "fixtures", "session-meta.json")


def session_meta_record(session_id, cwd):
    """Build a session_meta from a template captured from a real Codex session.

    Codex parses this record to repair thread metadata, and falls back to empty
    values when it fails to deserialise - so a hand-written stand-in silently
    exercises the failure path. Regenerate with tests/make-fixture.py after a
    Codex upgrade.
    """
    with open(FIXTURE_META, encoding="utf-8") as fh:
        blob = fh.read()
    blob = blob.replace("{{CWD}}", json.dumps(cwd)[1:-1])
    blob = blob.replace("{{SESSION_ID}}", session_id)
    blob = blob.replace("{{WINDOW_ID}}", "019f0000-0000-7000-8000-0000000000ff")
    blob = blob.replace("{{BASE_INSTRUCTIONS}}", "test base instructions")
    return json.loads(blob)


def make_rollout(home, session_id, cwd, body_path=None):
    """Write a rollout whose session_meta *and body* reference a project path.

    The body references matter: codex-mv must leave them alone, and a fixture
    without them cannot detect a tool that wrongly rewrites them.
    """
    day = os.path.join(home, "sessions", "2026", "08", "01")
    os.makedirs(day, exist_ok=True)
    path = os.path.join(day, f"rollout-2026-08-01T00-00-00-{session_id}.jsonl")
    body_path = body_path or cwd
    entries = [
        session_meta_record(session_id, cwd),
        {"timestamp": "2026-08-01T00:00:01.000Z", "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text",
                                  "text": f"<environment_context>\n  <cwd>{body_path}</cwd>\n</environment_context>"}]}},
        {"timestamp": "2026-08-01T00:00:02.000Z", "type": "turn_context",
         "payload": {"turn_id": "t1", "cwd": body_path}},
        # a real user event: without one the session reads as non-interactive
        # and the resume picker's default filter hides it
        {"timestamp": "2026-08-01T00:00:02.500Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "list the files here",
                     "images": [], "text_elements": []}},
        {"timestamp": "2026-08-01T00:00:03.000Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text",
                                  "text": f"I ran ls in {body_path}"}]}},
    ]
    with open(path, "w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
    return path


def make_db(home, rows):
    """rows: list of (session_id, rollout_path, cwd)."""
    db = os.path.join(home, "state_5.sqlite")
    con = sqlite3.connect(db)
    with con:
        con.execute(THREADS_SCHEMA)
        for sid, rollout, cwd in rows:
            con.execute(
                "INSERT INTO threads (id, rollout_path, cwd) VALUES (?,?,?)",
                (sid, rollout, cwd),
            )
    con.close()
    return db


def provision_real_db(home, rows):
    """Let Codex create and migrate its own state DB, then insert rows into it.

    The behavioural tests need Codex's authentic schema: a hand-rolled table
    lacks the sqlx migration bookkeeping, and the app-server exits on startup
    when it cannot migrate.
    """
    env = dict(os.environ)
    env["CODEX_HOME"] = home
    proc = subprocess.Popen(
        ["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, env=env, text=True, bufsize=1)
    try:
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"clientInfo": {"name": "codex-mv-tests", "title": "t",
                                      "version": "0"}}}) + "\n")
        proc.stdin.flush()
        proc.stdout.readline()  # wait until the DB has been created + migrated
    finally:
        proc.kill()
        proc.wait()
    time.sleep(0.5)  # let the WAL lock clear

    db = None
    for name in sorted(os.listdir(home)):
        if name.startswith("state_") and name.endswith(".sqlite"):
            db = os.path.join(home, name)
    if db is None:
        raise AssertionError("codex did not create a state DB")

    con = sqlite3.connect(db, timeout=15)
    try:
        cols = {r[1]: r for r in con.execute("PRAGMA table_info(threads)")}
        for sid, rollout, cwd in rows:
            values = {"id": sid, "rollout_path": rollout, "cwd": cwd,
                      "title": "", "preview": "synthetic test thread",
                      "source": "cli", "model_provider": "openai",
                      "sandbox_policy": '{"type":"workspace-write"}',
                      "approval_mode": "on-request", "cli_version": "0.146.0",
                      "first_user_message": "synthetic test thread",
                      "created_at": 1785600000, "updated_at": 1785600000,
                      "recency_at": 1785600000}
            # satisfy any other NOT NULL column that has no default
            for name, info in cols.items():
                if name in values:
                    continue
                _cid, _name, ctype, notnull, default, _pk = info
                if notnull and default is None:
                    values[name] = 0 if "INT" in (ctype or "").upper() else ""
            names = [n for n in values if n in cols]
            # Codex may already have backfilled this thread from the rollout
            # during startup; converge on our values either way.
            con.execute(
                f"INSERT OR REPLACE INTO threads ({','.join(names)}) "
                f"VALUES ({','.join('?' for _ in names)})",
                [values[n] for n in names])
        con.commit()
    finally:
        con.close()
    return db


def make_config(home, project_paths):
    path = os.path.join(home, "config.toml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('model = "gpt-5"\n\n')
        fh.write("# a comment that must survive\n")
        for p in project_paths:
            fh.write(f'[projects."{p}"]\ntrust_level = "trusted"\n\n')
    return path


def meta_cwd(rollout_path):
    with open(rollout_path, encoding="utf-8") as fh:
        return json.loads(fh.readline())["payload"]["cwd"]


def db_cwds(db):
    con = sqlite3.connect(db)
    try:
        return sorted(r[0] for r in con.execute("SELECT cwd FROM threads"))
    finally:
        con.close()


class Fixture:
    """A synthetic CODEX_HOME plus a project directory, isolated in a tmpdir."""

    SESSION_ID = "019f0000-0000-7000-8000-000000000001"

    def __init__(self, tmp, project="proj", sub=None, real_db=False):
        self.tmp = tmp
        self.home = os.path.join(tmp, "codex-home")
        os.makedirs(self.home, exist_ok=True)
        self.old = os.path.join(tmp, project)
        self.new = os.path.join(tmp, project + "-renamed")
        os.makedirs(self.old, exist_ok=True)
        with open(os.path.join(self.old, "marker.txt"), "w") as fh:
            fh.write("marker\n")
        cwd = os.path.join(self.old, sub) if sub else self.old
        if sub:
            os.makedirs(cwd, exist_ok=True)
        self.session_cwd = cwd
        self.config = make_config(self.home, [self.old])
        self.rollout = make_rollout(self.home, self.SESSION_ID, cwd)
        build = provision_real_db if real_db else make_db
        self.db = build(self.home, [(self.SESSION_ID, self.rollout, cwd)])

    def run(self, *extra):
        return run_codex_mv("--codex-home", self.home, "-y", "--no-color",
                            self.old, self.new, *extra)


class StructuralTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="codex-mv-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_happy_path_updates_all_three_stores(self):
        f = Fixture(self.tmp)
        r = f.run()
        self.assertEqual(r.returncode, 0, r.stderr)

        self.assertTrue(os.path.isdir(f.new), "directory should have moved")
        self.assertFalse(os.path.exists(f.old))
        self.assertTrue(os.path.exists(os.path.join(f.new, "marker.txt")))

        self.assertEqual(meta_cwd(f.rollout), f.new)
        self.assertEqual(db_cwds(f.db), [f.new])
        with open(f.config, encoding="utf-8") as fh:
            cfg = fh.read()
        self.assertIn(f'[projects."{f.new}"]', cfg)
        self.assertNotIn(f'[projects."{f.old}"]', cfg)
        self.assertIn("trust_level", cfg)
        self.assertIn("a comment that must survive", cfg)

    def test_transcript_body_is_left_untouched(self):
        """The body is a historical record; Codex re-announces cwd on resume."""
        f = Fixture(self.tmp)
        f.run()
        with open(f.rollout, encoding="utf-8") as fh:
            lines = fh.readlines()
        body = "".join(lines[1:])
        self.assertIn(f.old, body, "stale body refs must be preserved")
        self.assertNotIn(f.new, body, "codex-mv must not rewrite history")

    def test_subdirectory_sessions_are_remapped(self):
        """A session recorded in OLD/sub must land in NEW/sub."""
        f = Fixture(self.tmp, sub="nested/deeper")
        r = f.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        expected = os.path.join(f.new, "nested/deeper")
        self.assertEqual(meta_cwd(f.rollout), expected)
        self.assertEqual(db_cwds(f.db), [expected])

    def test_sibling_prefix_paths_are_not_touched(self):
        """Renaming /x/proj must not affect /x/proj-other."""
        f = Fixture(self.tmp)
        other = os.path.join(self.tmp, "proj-other")
        os.makedirs(other, exist_ok=True)
        other_rollout = make_rollout(
            f.home, "019f0000-0000-7000-8000-000000000002", other)
        con = sqlite3.connect(f.db)
        with con:
            con.execute("INSERT INTO threads (id, rollout_path, cwd) VALUES (?,?,?)",
                        ("019f0000-0000-7000-8000-000000000002", other_rollout, other))
        con.close()
        with open(f.config, "a", encoding="utf-8") as fh:
            fh.write(f'[projects."{other}"]\ntrust_level = "trusted"\n')

        f.run()

        self.assertEqual(meta_cwd(other_rollout), other, "sibling must be untouched")
        self.assertIn(other, db_cwds(f.db))
        self.assertTrue(os.path.isdir(other))
        with open(f.config, encoding="utf-8") as fh:
            self.assertIn(f'[projects."{other}"]', fh.read())

    def test_dry_run_changes_nothing(self):
        f = Fixture(self.tmp)
        before = {
            "meta": meta_cwd(f.rollout),
            "db": db_cwds(f.db),
            "cfg": open(f.config, encoding="utf-8").read(),
        }
        r = run_codex_mv("--codex-home", f.home, "--dry-run", "--no-color",
                         f.old, f.new)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isdir(f.old), "dry run must not move the directory")
        self.assertFalse(os.path.exists(f.new))
        self.assertEqual(meta_cwd(f.rollout), before["meta"])
        self.assertEqual(db_cwds(f.db), before["db"])
        self.assertEqual(open(f.config, encoding="utf-8").read(), before["cfg"])
        self.assertIn("Dry-run complete", r.stderr)

    def backup_dir(self, f):
        root = os.path.join(f.home, ".codex-mv-backups")
        self.assertTrue(os.path.isdir(root), "no backup directory")
        stamps = os.listdir(root)
        self.assertEqual(len(stamps), 1)
        return os.path.join(root, stamps[0])

    def test_backup_records_a_manifest_not_transcript_copies(self):
        f = Fixture(self.tmp)
        f.run()
        backup = self.backup_dir(f)

        with open(os.path.join(backup, "manifest.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(manifest["old_path"], f.old)
        self.assertEqual(manifest["new_path"], f.new)
        self.assertEqual(len(manifest["rollouts"]), 1)
        # the manifest must hold the ORIGINAL cwd, so the edit can be reversed
        self.assertEqual(manifest["rollouts"][0]["cwd"], f.session_cwd)
        self.assertEqual(manifest["rollouts"][0]["path"], f.rollout)

        self.assertTrue(os.path.exists(os.path.join(backup, "state_5.sqlite")))
        self.assertTrue(os.path.exists(os.path.join(backup, "config.toml")))

        # transcripts must NOT be copied: on a real project that is ~570 MB
        self.assertFalse(os.path.exists(os.path.join(backup, "sessions")))
        total = sum(os.path.getsize(os.path.join(backup, n))
                    for n in os.listdir(backup))
        self.assertLess(total, os.path.getsize(f.rollout) + 200_000,
                        "backup should not scale with transcript size")

    def test_undo_restores_everything(self):
        f = Fixture(self.tmp)
        before_cfg = open(f.config, encoding="utf-8").read()
        f.run()
        backup = self.backup_dir(f)

        r = run_codex_mv("--undo", backup, "-y", "--no-color")
        self.assertEqual(r.returncode, 0, r.stderr)

        self.assertTrue(os.path.isdir(f.old), "directory should be back")
        self.assertFalse(os.path.exists(f.new))
        self.assertTrue(os.path.exists(os.path.join(f.old, "marker.txt")))
        self.assertEqual(meta_cwd(f.rollout), f.session_cwd)
        self.assertEqual(db_cwds(f.db), [f.session_cwd])
        self.assertEqual(open(f.config, encoding="utf-8").read(), before_cfg)

    def test_undo_restores_subdirectory_sessions(self):
        f = Fixture(self.tmp, sub="nested/deeper")
        f.run()
        backup = self.backup_dir(f)
        r = run_codex_mv("--undo", backup, "-y", "--no-color")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(meta_cwd(f.rollout), f.session_cwd)
        self.assertEqual(db_cwds(f.db), [f.session_cwd])

    def test_undo_refuses_without_a_manifest(self):
        empty = os.path.join(self.tmp, "not-a-backup")
        os.makedirs(empty, exist_ok=True)
        r = run_codex_mv("--undo", empty, "-y", "--no-color")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("No manifest.json", r.stderr)

    def test_dry_run_previews_the_backup(self):
        """The backup is the expensive part; you should see it before running.

        Asserts the reported figures against the real files, not just that the
        labels appear - a preview that prints plausible-looking numbers it did
        not measure is worse than none.
        """
        f = Fixture(self.tmp)
        # pad the transcript so its size is unmistakable in the output
        with open(f.rollout, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "event_msg",
                                 "payload": {"type": "token_count",
                                             "pad": "x" * 300_000}}) + "\n")
        rollout_bytes = os.path.getsize(f.rollout)
        db_bytes = os.path.getsize(f.db)

        r = run_codex_mv("--codex-home", f.home, "--dry-run", "--no-color",
                         f.old, f.new)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Backup", r.stderr)
        self.assertIn("manifest.json (1 entries)", r.stderr)

        self.assertIn(codex_mv_module().human_size(rollout_bytes), r.stderr,
                      "transcript size must be measured, not invented")
        self.assertIn("transcripts are not copied", r.stderr)
        self.assertIn(codex_mv_module().human_size(db_bytes), r.stderr,
                      "state DB size must be measured")

    def test_backup_preview_measures_real_sizes(self):
        mod = codex_mv_module()
        f = Fixture(self.tmp)
        preview = mod.backup_preview(
            f.home, [(f.rollout, f.session_cwd)], f.db, f.config, [(0, f.old)])
        self.assertEqual(preview["transcripts"], os.path.getsize(f.rollout))
        self.assertGreaterEqual(preview["total"], os.path.getsize(f.db))
        # the manifest must be far smaller than the transcripts it describes
        self.assertLess(preview["total"] - os.path.getsize(f.db), 10_000)

    def test_guard_groups_processes_into_sessions(self):
        """One Codex session is several processes; report sessions, not pids."""
        f = Fixture(self.tmp)
        inner_dir = os.path.join(self.tmp, "inner")
        outer_dir = os.path.join(self.tmp, "outer")
        os.makedirs(inner_dir, exist_ok=True)
        os.makedirs(outer_dir, exist_ok=True)
        inner = os.path.join(inner_dir, "codex")
        outer = os.path.join(outer_dir, "codex")
        with open(inner, "w") as fh:
            fh.write("#!/bin/sh\nsleep 30\n")
        # the outer process stays alive with the inner one as its child,
        # mirroring codex's node wrapper plus platform binary
        with open(outer, "w") as fh:
            fh.write(f'#!/bin/sh\n"{inner}" &\nwait\n')
        for path in (inner, outer):
            os.chmod(path, 0o755)
        proc = subprocess.Popen([outer], cwd=f.old,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(0.5)
            r = f.run()
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("1 session(s), 2 process(es)", r.stderr)
        finally:
            proc.kill()
            proc.wait()

    def test_refuses_when_codex_is_live_in_the_project(self):
        f = Fixture(self.tmp)
        fake_bin = os.path.join(self.tmp, "fakebin")
        os.makedirs(fake_bin, exist_ok=True)
        fake = os.path.join(fake_bin, "codex")
        with open(fake, "w") as fh:
            fh.write("#!/bin/sh\nsleep 30\n")
        os.chmod(fake, 0o755)
        proc = subprocess.Popen([fake], cwd=f.old,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(0.3)
            r = f.run()
            self.assertNotEqual(r.returncode, 0, "should refuse while codex is live")
            self.assertIn("Codex is running", r.stderr)
            self.assertTrue(os.path.isdir(f.old), "must not move the directory")
            self.assertEqual(meta_cwd(f.rollout), f.old)
        finally:
            proc.kill()
            proc.wait()

    def test_dry_run_only_warns_when_codex_is_live(self):
        """A dry run writes nothing, so a live session must not block it."""
        f = Fixture(self.tmp)
        fake_bin = os.path.join(self.tmp, "fakebin2")
        os.makedirs(fake_bin, exist_ok=True)
        fake = os.path.join(fake_bin, "codex")
        with open(fake, "w") as fh:
            fh.write("#!/bin/sh\nsleep 30\n")
        os.chmod(fake, 0o755)
        proc = subprocess.Popen([fake], cwd=f.old,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(0.3)
            r = run_codex_mv("--codex-home", f.home, "--dry-run", "--no-color",
                             f.old, f.new)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("a real run would refuse", r.stderr)
            self.assertIn("Dry-run complete", r.stderr)
            self.assertTrue(os.path.isdir(f.old))
            self.assertEqual(meta_cwd(f.rollout), f.old)
        finally:
            proc.kill()
            proc.wait()

    def test_missing_source(self):
        f = Fixture(self.tmp)
        r = run_codex_mv("--codex-home", f.home, "-y", "--no-color",
                         os.path.join(self.tmp, "nope"), f.new)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Source directory not found", r.stderr)

    def test_existing_destination(self):
        f = Fixture(self.tmp)
        os.makedirs(f.new, exist_ok=True)
        r = f.run()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Destination already exists", r.stderr)

    def test_identical_paths(self):
        f = Fixture(self.tmp)
        r = run_codex_mv("--codex-home", f.home, "-y", "--no-color", f.old, f.old)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("identical", r.stderr)

    def test_destination_inside_source(self):
        f = Fixture(self.tmp)
        r = run_codex_mv("--codex-home", f.home, "-y", "--no-color",
                         f.old, os.path.join(f.old, "inner"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("cannot be inside", r.stderr)

    def test_project_with_no_sessions_still_renames(self):
        tmp = self.tmp
        home = os.path.join(tmp, "empty-home")
        os.makedirs(home, exist_ok=True)
        old = os.path.join(tmp, "lonely")
        new = os.path.join(tmp, "lonely-renamed")
        os.makedirs(old, exist_ok=True)
        r = run_codex_mv("--codex-home", home, "-y", "--no-color", old, new)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isdir(new))
        self.assertIn("No Codex sessions recorded", r.stderr)


# ------------------------------------------------------- behavioural layer ----
def codex_available():
    return shutil.which("codex") is not None


class AppServer:
    """Minimal JSON-RPC client for `codex app-server`."""

    def __init__(self, codex_home):
        env = dict(os.environ)
        env["CODEX_HOME"] = codex_home
        self.p = subprocess.Popen(
            ["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env, text=True, bufsize=1)
        self.q = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self._id = 0

    def _pump(self):
        for line in self.p.stdout:
            self.q.put(line)
        self.q.put(None)

    def _send(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        rid = None
        if not notify:
            self._id += 1
            rid = self._id
            msg["id"] = rid
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()
        return rid

    def call(self, method, params=None, timeout=90):
        rid = self._send(method, params)
        end = time.time() + timeout
        while time.time() < end:
            try:
                line = self.q.get(timeout=1)
            except queue.Empty:
                continue
            if line is None:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == rid:
                return msg.get("result", msg)
        raise AssertionError(f"no response to {method}")

    def handshake(self):
        self.call("initialize",
                  {"clientInfo": {"name": "codex-mv-tests", "title": "t", "version": "0"}})
        self._send("initialized", notify=True)

    def list_cwd(self, cwd, state_db_only=False):
        params = {"limit": 50, "cwd": cwd}
        if state_db_only:
            params["useStateDbOnly"] = True
        res = self.call("thread/list", params)
        return res.get("data", res.get("threads", []))

    def close(self):
        try:
            self.p.kill()
        except OSError:
            pass


@unittest.skipUnless(codex_available(), "codex CLI not installed")
class BehaviouralTests(unittest.TestCase):
    """Does `codex resume` actually find the sessions after a rename?

    thread/list with a cwd filter is precisely what the resume picker runs.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="codex-mv-behave-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_sessions_follow_the_project_after_rename(self):
        f = Fixture(self.tmp, real_db=True)
        srv = AppServer(f.home)
        self.addCleanup(srv.close)
        srv.handshake()

        self.assertEqual(len(srv.list_cwd(f.old)), 1, "baseline: found at old path")

        r = f.run()
        self.assertEqual(r.returncode, 0, r.stderr)

        srv2 = AppServer(f.home)
        self.addCleanup(srv2.close)
        srv2.handshake()
        self.assertEqual(len(srv2.list_cwd(f.new)), 1,
                         "session must be listed under the new path")
        self.assertEqual(len(srv2.list_cwd(f.old)), 0,
                         "session must no longer be listed under the old path")

    def test_state_db_only_path_also_updated(self):
        """Guards the failure mode where only the rollout is rewritten."""
        f = Fixture(self.tmp, real_db=True)
        r = f.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        srv = AppServer(f.home)
        self.addCleanup(srv.close)
        srv.handshake()
        self.assertEqual(len(srv.list_cwd(f.new, state_db_only=True)), 1,
                         "stale sqlite would break the state-DB-only path")
        self.assertEqual(len(srv.list_cwd(f.old, state_db_only=True)), 0)

    def test_subdirectory_session_listed_under_new_subpath(self):
        f = Fixture(self.tmp, sub="nested", real_db=True)
        r = f.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        srv = AppServer(f.home)
        self.addCleanup(srv.close)
        srv.handshake()
        self.assertEqual(len(srv.list_cwd(os.path.join(f.new, "nested"))), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
