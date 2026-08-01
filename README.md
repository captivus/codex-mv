# `codex-mv`

Rename a project directory **and** keep its Codex sessions attached to it.

```bash
codex-mv ~/projects/old-name ~/projects/new-name
```

## What problem this solves

`codex resume` only offers you sessions belonging to the directory you launch it
from. Rename the directory and those sessions vanish from the picker — the work
is still on disk, but Codex no longer associates it with the project.

This is the same class of problem [`claude-mv`](https://github.com/captivus/claude-mv)
solves for Claude Code, but the mechanism is completely different.

## How Codex stores this (and why `mv` is not enough)

Claude Code keys history by path: `~/.claude/projects/-home-alice-code-myproject/`.
The path *is* the directory name, so moving a folder is the whole job.

**Codex has no per-project directory at all.** Sessions live in a date-bucketed
tree and record which project they belong to *inside* each file:

```
~/.codex/sessions/2026/08/01/rollout-2026-08-01T07-19-58-<uuid>.jsonl
   line 1: {"type":"session_meta","payload":{"cwd":"/home/alice/code/myproject", ...}}
```

That `cwd` is mirrored into a sqlite cache (`state_5.sqlite`, table `threads`),
and a third place records the project's trust level (`config.toml`). A rename
has to update all three:

| Store | Field | Why it matters |
|---|---|---|
| `sessions/**/rollout-*.jsonl` | `session_meta.cwd` | the source of truth |
| `state_<N>.sqlite` | `threads.cwd` | what the picker reads |
| `config.toml` | `[projects."<path>"]` | trust level / sandbox |

Updating only the sqlite row does not work: Codex re-reads the rollout and
repairs the row straight back to the old path. Updating only the rollout leaves
a stale cache that still surfaces through Codex's state-DB-only read path. Both
are required, which is what this tool does.

## What it deliberately does **not** change

The old path also appears throughout the transcript body — in recorded shell
commands, their output, and earlier `<environment_context>` blocks. `codex-mv`
leaves every one of those alone.

That is safe because when you resume, Codex injects a **fresh
`<environment_context>` announcing the current directory**, and records a
per-turn `turn_context` with it. The resumed session runs in the new directory
and the model is told so explicitly. The stale text is a historical record of
what actually happened; rewriting it would make the transcript claim commands
ran somewhere they never did.

## Usage

```bash
codex-mv [options] OLD_PATH NEW_PATH
```

| Flag | Description |
|------|-------------|
| `-n`, `--dry-run` | Show the plan — including which sessions would be repointed — and change nothing |
| `-y`, `--yes` | Skip the confirmation prompt |
| `--codex-home PATH` | Use `PATH` instead of `$CODEX_HOME` / `~/.codex` |
| `--undo DIR` | Reverse a previous run, using the backup directory it printed |
| `--no-backup` | Skip the pre-flight backup (not recommended) |
| `--no-color` | Disable coloured output |
| `-h`, `--help` | Show help |
| `--version` | Print the version |

Always worth a dry run first:

```bash
codex-mv --dry-run ~/projects/old-name ~/projects/new-name
```

```
Planned actions:
  Project dir  : /home/alice/projects/old-name -> /home/alice/projects/new-name
  Sessions     : 3 rollout file(s) to repoint
                 rollout-2026-07-22T15-32-17-019f8b87-....jsonl  (/home/alice/projects/old-name)
                 ...
  State DB     : 3 thread row(s) in state_5.sqlite
  Trust entries: 1 in config.toml
  Backup       : /home/alice/.codex/.codex-mv-backups/<timestamp>
                 manifest.json (3 entries) + state_5.sqlite 9.6 MB + config.toml 4.0 KB  ~= 9.6 MB
                 transcripts are not copied (545.3 MB left in place)
Dry-run complete - no changes made.
```

## Safety

Codex writes to the state database and appends to rollout files *continuously*
while a session is open. Editing them underneath a running Codex risks losing or
corrupting writes — a hazard `claude-mv` simply does not have, since it only
moves a folder.

So `codex-mv`:

1. **Refuses to run** if a Codex session is working inside the target
   directory, and tells you enough to find it. One session is several
   processes -- a node wrapper, the platform binary, anything it spawned -- so
   they are grouped into sessions, each labelled with the terminal it is
   attached to, when it started, and what the conversation is about:

   ```
   ! Codex is running in this project - a real run would refuse:
     2 session(s), 5 process(es):
       [1] pts/20  started 2026-07-28 14:24  pid 684732 (+1 more: 684742)
           cwd     : /home/alice/projects/my-project
           session : 019f8679  last active 2026-07-28 14:57:02
           about   : "So I'm building a pickleball league. It's called Kitchen..."
   ```

   The conversation label comes from the state DB, matched to the process via
   the rollout file it holds open.
2. **Records how to reverse itself first**, into
   `~/.codex/.codex-mv-backups/<timestamp>/`, and prints the path. The state DB
   (plus `-wal`/`-shm`) and `config.toml` are copied in full; the sessions are
   captured as a `manifest.json` listing each rollout and its original `cwd`.

   Transcripts are deliberately *not* copied. Only one field on line 1 changes,
   and rollouts are written atomically, so a full copy protects against nothing
   the manifest does not — and on a real project the difference is large: 110
   sessions of chat history is ~570 MB, against a manifest of a few tens of KB.

   Reverse a run with `codex-mv --undo ~/.codex/.codex-mv-backups/<timestamp>`.
   It restores each session's recorded `cwd` exactly, moves the directory back,
   and remaps the state DB and trust entry — remapping rather than restoring the
   copies, so unrelated work done since the rename is not discarded.
3. **Writes atomically**, via a temp file and `os.replace`, so an interrupted run
   cannot leave a half-written rollout.
4. Refuses identical paths, a missing source, an existing destination, or a
   destination nested inside the source.

Sessions recorded in a *subdirectory* of the project (`OLD/sub`) are remapped to
`NEW/sub`. Sibling paths that merely share a prefix (`/x/proj-other` when
renaming `/x/proj`) are left alone.

## Installation

Standalone — Python 3.8+, standard library only:

```bash
curl -sSL https://raw.githubusercontent.com/captivus/codex-mv/main/codex-mv -o ~/bin/codex-mv
chmod +x ~/bin/codex-mv
```

## Tests

```bash
uv run --no-project python tests/test_codex_mv.py
```

Every test builds a throwaway `CODEX_HOME` in a temp directory; nothing reads or
writes your real `~/.codex`. Two layers:

- **Structural** — asserts the files, DB rows, and config entries that change,
  and the ones that must not.
- **Behavioural** — drives the real `codex app-server` and asserts the renamed
  project's sessions come back from `thread/list`, which is exactly what the
  resume picker filters on. Skipped automatically if the `codex` CLI is absent.

`tests/fixtures/session-meta.json` is captured from a genuine Codex session by
`tests/make-fixture.py`. This matters: Codex falls back to empty metadata when a
`session_meta` record fails to deserialise, so a hand-written stand-in quietly
tests the failure path instead. Re-run `./tests/make-fixture.py` if a Codex
upgrade changes that schema. The committed fixture keeps only the *shape* Codex
needs to parse: the verbose `base_instructions` text is replaced with a
placeholder, and identifiers are templated, so no captured session content is
carried in this repo.

### What is verified, and what is not

Verified automatically, on every run:

- the three stores are updated, and the transcript body is not
- subdirectory sessions are remapped; sibling paths sharing a prefix are not
- dry run changes nothing (checked by checksum)
- the live-session guard refuses a real run and only warns on a dry run
- the backup records a reversible manifest rather than copying transcripts
- the dry run previews the backup, with sizes measured from the real files
- the guard groups processes into the sessions you would actually close
- **`--undo` puts everything back**: directory, every session's recorded `cwd`,
  the state DB rows, and the trust entry
- **the renamed project's sessions come back from `thread/list`**, through both
  the default scan-and-repair path and `useStateDbOnly` — driven against a real
  `codex app-server`

The suite has been mutation-tested: skipping the sqlite update, skipping the
rollout rewrite, dropping the path-separator boundary check, rewriting the whole
file, breaking the undo restore, and recording the wrong `cwd` in the manifest
each cause failures in the tests that should catch them.

Verified once, by hand, not automated:

- that a *resumed* session runs in the new directory and the model is told so.
  Confirmed against Codex 0.146.0 by renaming a project whose transcript still
  referenced the old path in 46 places, resuming it, and asking the model where
  it was. It injected a fresh `<environment_context>` naming the new directory,
  ran its commands there, and found a file that existed only in the renamed
  copy.

There is deliberately no automated live end-to-end test. Driving a complete
session through the app-server needs more of its protocol than this tool
warrants: `thread/start` creates an in-memory thread with no rollout on disk, so
it cannot then be resumed, and turn events did not reach a plain stdio client.
The behaviour above is covered by the manual check; the automated suite covers
everything that does not require a model call.

## Compatibility

Developed and verified against **Codex CLI 0.146.0** (state DB `state_5.sqlite`).
The state DB filename is versioned, so the tool picks the highest `state_<N>.sqlite`
present rather than hardcoding one.
