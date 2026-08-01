#!/usr/bin/env python3
"""Regenerate tests/fixtures/session-meta.json from a real Codex session.

Codex repairs thread metadata by parsing `session_meta` out of the rollout, and
it silently falls back to empty values when that record does not deserialise.
A hand-written session_meta is therefore not a faithful fixture: tests built on
one exercise the repair's failure path instead of its success path.

So we capture a genuine session_meta once, template the values that vary, and
commit the result. This starts ONE minimal turn in a throwaway CODEX_HOME --
enough for Codex to write a rollout, which is all we need; the turn does not
have to finish, and this script does not wait for a model reply.
Re-run it when a Codex upgrade changes the session_meta schema.

    ./tests/make-fixture.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
TARGET = os.path.join(FIXTURES, "session-meta.json")

sys.path.insert(0, HERE)
from test_codex_mv import AppServer  # noqa: E402


def main():
    if not shutil.which("codex"):
        print("codex CLI not found", file=sys.stderr)
        return 1

    tmp = tempfile.mkdtemp(prefix="codex-mv-fixture-")
    try:
        home = os.path.join(tmp, "home")
        proj = os.path.join(tmp, "proj")
        os.makedirs(home)
        os.makedirs(proj)
        with open(os.path.join(home, "config.toml"), "w") as fh:
            fh.write('model = "gpt-5"\n\n')
            fh.write(f'[projects."{proj}"]\ntrust_level = "trusted"\n')

        srv = AppServer(home)
        try:
            srv.handshake()
            started = srv.call("thread/start", {"cwd": proj})
            tid = (started.get("thread") or started).get("id")
            print(f"started thread {tid}")

            srv._send("turn/start", {
                "threadId": tid,
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "readOnly"},
                "input": [{"type": "text", "text": "Reply with the single word: ok"}],
            })
            deadline = time.time() + 240
            while time.time() < deadline:
                try:
                    line = srv.q.get(timeout=2)
                except Exception:  # noqa: BLE001
                    continue
                if line is None:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                payload = (msg.get("params") or {}).get("payload") or {}
                if payload.get("type") in ("task_complete", "turn_aborted"):
                    print(f"turn finished: {payload.get('type')}")
                    break
        finally:
            srv.close()
        time.sleep(1)

        rollouts = []
        for root, _dirs, files in os.walk(os.path.join(home, "sessions")):
            for name in files:
                if name.endswith(".jsonl"):
                    rollouts.append(os.path.join(root, name))
        if not rollouts:
            print("no rollout was written - the turn did not persist", file=sys.stderr)
            return 1

        with open(rollouts[0], encoding="utf-8") as fh:
            meta = json.loads(fh.readline())
        if meta.get("type") != "session_meta":
            print("first record was not session_meta", file=sys.stderr)
            return 1

        # template the values each test supplies for itself
        blob = json.dumps(meta)
        blob = blob.replace(proj, "{{CWD}}").replace(tid, "{{SESSION_ID}}")
        meta = json.loads(blob)

        # Only the record's *shape* matters for the repair to deserialise, so
        # drop the bulky vendor prompt text and any residual identifier rather
        # than committing captured session content to a public repo.
        payload = meta.get("payload") or {}
        if isinstance(payload.get("base_instructions"), dict):
            payload["base_instructions"] = {"text": "{{BASE_INSTRUCTIONS}}"}
        if isinstance(payload.get("context_window"), dict):
            payload["context_window"] = {"window_id": "{{WINDOW_ID}}"}

        os.makedirs(FIXTURES, exist_ok=True)
        with open(TARGET, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=1, sort_keys=True)
            fh.write("\n")
        print(f"wrote {TARGET}")
        print(f"payload keys: {sorted((meta.get('payload') or {}).keys())}")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
