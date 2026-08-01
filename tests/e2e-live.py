#!/usr/bin/env python3
"""End-to-end check: a real Codex session survives a codex-mv rename.

Not part of the default suite - this spends model tokens. It closes the one
gap the offline tests cannot: they prove codex-mv produces the right on-disk
state, but not that a *live resumed session* then behaves correctly.

Runs entirely inside a throwaway CODEX_HOME:

  1. create a genuine session in OLD (one model turn, so a rollout is written)
  2. rename OLD -> NEW with codex-mv
  3. resume that same session and run another turn
  4. assert the model reports the NEW directory and can see a file that
     exists only there

    ./tests/e2e-live.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CODEX_MV = os.path.join(os.path.dirname(HERE), "codex-mv")
sys.path.insert(0, HERE)
from test_codex_mv import AppServer  # noqa: E402


def last_agent_message(home):
    """Read the newest assistant message straight out of the rollout.

    The event stream's shape varies by notification type; the rollout is the
    durable record, so read the answer from there rather than guessing at
    notification nesting.
    """
    newest, newest_mtime = None, -1
    for root, _dirs, files in os.walk(os.path.join(home, "sessions")):
        for name in files:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(root, name)
            mtime = os.path.getmtime(path)
            if mtime > newest_mtime:
                newest, newest_mtime = path, mtime
    if not newest:
        return None
    answer = None
    with open(newest, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            payload = obj.get("payload") or {}
            if payload.get("type") == "agent_message" and payload.get("message"):
                answer = payload["message"]
            elif payload.get("type") == "message" and payload.get("role") == "assistant":
                for chunk in payload.get("content", []):
                    if chunk.get("text"):
                        answer = chunk["text"]
    return answer


def run_turn(srv, tid, text, timeout=240):
    """Run one turn and return the agent's final message."""
    srv._send("turn/start", {
        "threadId": tid,
        "approvalPolicy": "never",
        "sandboxPolicy": {"type": "workspaceWrite"},
        "input": [{"type": "text", "text": text}],
    })
    answer = None
    deadline = time.time() + timeout
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
        if payload.get("type") == "agent_message":
            answer = payload.get("message")
        if payload.get("type") in ("task_complete", "turn_aborted"):
            break
    return answer


def main():
    if not shutil.which("codex"):
        print("codex CLI not found", file=sys.stderr)
        return 1

    tmp = tempfile.mkdtemp(prefix="codex-mv-e2e-")
    failures = []
    try:
        home = os.path.join(tmp, "home")
        old = os.path.join(tmp, "proj")
        new = os.path.join(tmp, "proj-renamed")
        os.makedirs(home)
        os.makedirs(old)
        with open(os.path.join(old, "marker.txt"), "w") as fh:
            fh.write("this file exists only in the renamed project\n")
        with open(os.path.join(home, "config.toml"), "w") as fh:
            fh.write('model = "gpt-5"\n\n')
            fh.write(f'[projects."{old}"]\ntrust_level = "trusted"\n')

        # 1. a genuine session in the original directory
        srv = AppServer(home)
        try:
            srv.handshake()
            started = srv.call("thread/start", {"cwd": old})
            tid = (started.get("thread") or started).get("id")
            print(f"[1] created session {tid} in {old}")
            run_turn(srv, tid, "Reply with the single word: ready")
        finally:
            srv.close()
        time.sleep(1)

        # 2. rename it
        print("[2] renaming with codex-mv")
        proc = subprocess.run(
            [sys.executable, CODEX_MV, "--codex-home", home, "-y", "--no-color",
             old, new],
            capture_output=True, text=True)
        print(proc.stderr.strip())
        if proc.returncode != 0:
            failures.append("codex-mv exited non-zero")

        # 3. resume the same session and work in it
        srv2 = AppServer(home)
        try:
            srv2.handshake()
            listed = srv2.list_cwd(new)
            print(f"[3] sessions listed under the new path: {len(listed)}")
            if len(listed) != 1:
                failures.append(f"expected 1 session under {new}, got {len(listed)}")

            resumed = srv2.call("thread/resume", {"threadId": tid})
            reported = (resumed.get("thread") or {}).get("cwd")
            print(f"    resumed thread reports cwd: {reported}")
            if reported != new:
                failures.append(f"resumed cwd was {reported}, expected {new}")

            answer = run_turn(
                srv2, tid,
                "Run pwd and ls. Then state the absolute directory you are "
                "working in, and whether marker.txt exists. One short sentence.")
            # the event stream's shape varies; the rollout is the durable record
            answer = answer or last_agent_message(home)
            print(f"[4] model said: {answer}")
            if not answer:
                failures.append("no answer from the resumed session")
            else:
                if new not in answer:
                    failures.append("model did not report the new directory")
                if os.path.basename(old) in answer.replace(new, ""):
                    failures.append("model referred to the old directory")
                if "marker.txt" not in answer:
                    failures.append("model did not confirm marker.txt")
        finally:
            srv2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("PASS: the session survived the rename and resumed in the new directory")
    return 0


if __name__ == "__main__":
    sys.exit(main())
