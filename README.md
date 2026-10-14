# cc-move

Move a project folder to a new path **without losing its Claude Code sessions**.

Claude Code ties every session to the folder's path. Move or rename the folder by hand and the session list in the
new location is empty; the VS Code extension also forgets which Claude tabs you had open. cc-move moves the folder
and brings all of that along:

- the folder itself, copied with robocopy and **verified file by file with SHA-256** before anything else changes;
- the session transcripts, subagents, tool results and memory in `~/.claude/projects/`, with every mention of the
  old path rewritten (including the `cwd` of each message);
- the per-project settings in `~/.claude.json` and the prompt history;
- the VS Code state of the folder, so the **Claude tabs you had open reappear** when you open the new path;
- the original, which is **kept** and renamed to `archive-<date>-<name>` so you can delete it later, once you are
  sure everything works.

It was built to move projects out of a leftover `OneDrive\Desktop` folder, but works for any move on the same
machine.

## Requirements

- **Windows** (it relies on robocopy, NTFS junctions and Windows paths).
- Python 3.8+ on the `PATH` (`python` or the `py` launcher). Standard library only.
- Claude Code (CLI or VS Code extension). Tested with Claude Code 2.1.299.

## Install

```
/plugin marketplace add stgoddv/cc-move
/plugin install cc-move@cc-move
```

## Use

1. Close the VS Code window of the project you are moving (other windows can stay open) and any Claude Code session
   running inside it.
2. From a Claude Code session opened **outside** that project, run:

   ```
   /cc-move:migrate C:\old\path\my-project C:\new\path\my-project
   ```

   Add `--with-deps` to also copy `node_modules` and virtual environments (see below).

The skill runs a dry run first and shows you what will change, then asks before migrating.

### Phase 1 — the script (deterministic)

1. Refuses to start if a Claude session or the project's VS Code window is still open.
2. Records the git status and the packages of each virtual environment.
3. Hashes the source, copies it with robocopy, hashes the copy and compares both manifests. **Any difference
   aborts** before Claude Code's state is touched.
4. Backs up `~/.claude.json`, the history and the session folders, then moves and rewrites them.
5. Copies the VS Code state of the folder to the one VS Code will use for the new path.
6. Renames the original to `archive-<date>-<name>` and writes a short note in the project's `CLAUDE.local.md`
   (ignored by git through `.git/info/exclude`), so a future agent knows the folder was moved.

### Phase 2 — the agent (judgment)

What a copy cannot carry over is audited and repaired by Claude: reinstalling dependencies from the lockfiles,
recreating virtual environments with the same Python version, fixing absolute paths in local config. A read-only
`auditor` subagent does the audit. Commits, pushes and deploys are forbidden during the migration (and blocked by a
hook for the rest of that session — open another session to commit).

### Dependencies

By default `node_modules` and virtual environments are **not copied**: they are usually most of the files and are
cheap to recreate. Use `--with-deps` when reinstalling is expensive or risky (CUDA wheels, old or native packages).
Copied pnpm junctions are repointed to the new folder. A copied virtual environment works through `python -m`, but
its `activate` scripts and `Scripts\*.exe` launchers still contain the old path; phase 2 repairs them.

### Progress

Large projects print at most one progress line per minute, e.g.
`37% | copying 12.1 GB of 48.2 GB | 9 min elapsed, ~15 min left`, and keep the latest line in `progress.txt` next
to the report. Small projects finish before the first line.

## Other commands

The script lives in the plugin at `skills/migrate/scripts/cc_move.py`; the skill prints its full path.

| Command | What it does |
|---|---|
| `verify --all` | Checks every migration and repairs the session links if something reverted them |
| `status` | Lists migrations and flags archives older than 6 months |
| `archive <dst>` | Retries renaming the original if it was in use |
| `undo <dst>` | Moves the sessions back to the old path and restores the original's name (keeps the copy) |
| `relink <src> <dst>` | Re-applies only the session reassociation |

Reports, the migration registry (`migrations.jsonl`) and progress files live in `%LOCALAPPDATA%\cc-move`, so they
survive plugin updates and uninstalls. Backups of Claude Code's state go to `~/.claude/backups/cc-move/`.

## How is this different from `/cd`?

`/cd <path>` moves the **current session** to another directory. It does not move the folder, the other sessions,
the project settings or the VS Code tabs. cc-move moves the folder and everything Claude Code keeps about it.

## Caveats

- Claude Code's and VS Code's storage formats are internal and undocumented; they may change. Each release states
  the Claude Code version it was tested with.
- When you open the new folder, VS Code asks whether you trust it (trust is stored per folder). Trusting the parent
  folder once avoids the question for every project you move there.
- Multi-root workspaces (`.code-workspace` files) are not handled.
- Users have reported that an open session can rewrite `~/.claude.json` from memory when it closes; this was not
  reproduced while testing, but `verify --all` repairs it if it happens.

## Development

```
python -m unittest discover -s tests -v     # tests use a synthetic ~/.claude and %APPDATA%
claude --plugin-dir ./plugin                 # try the plugin locally
```

See `CLAUDE.md` for the layout and the verified storage details.

## License

MIT
