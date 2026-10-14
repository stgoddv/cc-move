# cc-move

[![tests](https://github.com/stgoddv/cc-move/actions/workflows/test.yml/badge.svg)](https://github.com/stgoddv/cc-move/actions/workflows/test.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A [Claude Code](https://claude.com/claude-code) plugin that moves a project folder to a new path **without losing
its Claude Code sessions**. Windows only.

## Why

Claude Code ties every session to the folder's path. If you move or rename a project folder by hand, the session
list in the new location is empty, the per-project settings are gone and the VS Code extension forgets which Claude
tabs you had open. Your conversations are still on disk, just no longer linked to the folder.

cc-move moves the folder and brings all of that along:

- the folder itself, copied with robocopy and **verified file by file with SHA-256** before anything else changes;
- the session transcripts, subagents, tool results and memory in `~/.claude/projects/`, with every mention of the
  old path rewritten (including the working directory recorded in each message);
- the per-project settings in `~/.claude.json` and the prompt history;
- the VS Code state of the folder, so the **Claude tabs you had open reappear** when you open the new path;
- the original folder, which is **kept** and renamed to `archive-<date>-<name>` so you can delete it later, once you
  are sure everything works.

Then Claude audits the moved project and repairs what a copy cannot carry over (dependencies, absolute paths in
local config), without committing or deploying anything.

## Requirements

| What | How to check |
|---|---|
| Windows 10 or 11 | — |
| Python 3.8 or newer on the `PATH` (standard library only, nothing to `pip install`) | `python --version` in a terminal. If it is missing, install it from [python.org](https://www.python.org/downloads/) and tick "Add python.exe to PATH". |
| Claude Code, CLI or VS Code extension | `claude --version`. Tested with 2.1.299. |

## Install

In a Claude Code session, run these two commands:

```
/plugin marketplace add stgoddv/cc-move
/plugin install cc-move@cc-move
```

Or from a terminal:

```
claude plugin marketplace add stgoddv/cc-move
claude plugin install cc-move@cc-move
```

Restart Claude Code (or open a new session). Type `/cc-move` and the `/cc-move:migrate` command should appear.

## Move a project

Say you want to move `C:\Users\me\OneDrive\Desktop\my-app` to `C:\Users\me\code\my-app`.

1. **Close the project.** Close the VS Code window that has `my-app` open, and any Claude Code session running in
   it. Other VS Code windows can stay open.
2. **Open Claude Code somewhere else**, outside the project: for example in `C:\Users\me\code` (the destination's
   parent), or in another VS Code window.
3. **Run the command** with the current path and the new path (the new folder must not exist yet, or be empty; its
   parent must exist):

   ```
   /cc-move:migrate C:\Users\me\OneDrive\Desktop\my-app C:\Users\me\code\my-app
   ```

4. **Review the dry run.** Claude first shows what would change (files, sessions, settings, open tabs) without
   touching anything, and asks you to confirm.
5. **Confirm.** Claude runs the migration (Claude Code asks your permission for that command) and then audits and
   repairs the moved project. At the end it gives you a summary: what moved, what was recreated, what to check.
6. **Open the new folder in VS Code.** The Claude tabs you had open reappear and the session history is complete.
   VS Code asks whether you trust the folder; trusting the parent folder (`C:\Users\me\code`) once avoids the
   question for every project you move there.

To commit afterwards, open a new Claude Code session: during a migration a safety hook blocks `git commit`,
`git push` and common deploy commands for the rest of that session.

When you are sure everything works (cc-move suggests six months), delete the `archive-<date>-my-app` folder and the
`cc-move` section of `my-app\CLAUDE.local.md`.

### Several projects

Give Claude the list in one session (`/cc-move:migrate` and then the pairs of paths, one project per line). It
moves them one at a time and stops at the first failure.

## What happens, step by step

### Phase 1: the script (deterministic)

1. Refuses to start if a Claude Code session or the project's VS Code window is still open, if the destination
   exists or if there is not enough disk space.
2. Records the git status and the packages of each Python virtual environment.
3. Hashes the source, copies it with robocopy, hashes the copy and compares both lists. **Any difference stops the
   migration** before Claude Code's state is touched.
4. Backs up `~/.claude.json`, the prompt history and the session folders, then moves and rewrites them.
5. Copies the VS Code state of the folder to the one VS Code will use for the new path.
6. Renames the original to `archive-<date>-<name>` and writes a short note in the project's `CLAUDE.local.md`
   (kept out of git through `.git/info/exclude`), so a future agent knows the folder was moved.

### Phase 2: Claude (judgment)

A read-only auditor subagent looks for what the copy could not carry over: dependencies to reinstall, old absolute
paths in config files, links Windows did not allow to create. Claude then reinstalls dependencies **from the
lockfiles** (`npm ci`, `pnpm install --frozen-lockfile`…), recreates virtual environments with the same Python
version and fixes old paths in local configuration. It never commits, pushes, deploys, changes dependency versions
or touches the archived original. Changes stay uncommitted for you to review.

### Dependencies

By default `node_modules` and virtual environments (`.venv`, `venv`…) are **not copied**: they are usually most of
the files and are cheap to recreate. Add `--with-deps` to copy them too when reinstalling is slow or risky (large
CUDA/PyTorch environments, old or native packages):

```
/cc-move:migrate C:\old\path\my-app C:\new\path\my-app --with-deps
```

Copied pnpm junctions are repointed to the new folder. A copied virtual environment works through `python -m`, but
its `activate` scripts and `Scripts\*.exe` launchers still contain the old path; phase 2 repairs them.

### Progress

Large projects print at most one progress line per minute, for example
`37% | copying 12.1 GB of 48.2 GB | 9 min elapsed, ~15 min left`, and keep the latest line in `progress.txt` next
to the report. Small projects finish before the first line.

## Undo and other commands

At the end of a migration the script prints its full path (it lives inside the installed plugin, under
`skills\migrate\scripts\cc_move.py`). Run these from a regular terminal, or ask Claude to run them:

| Command | What it does |
|---|---|
| `python <script> status` | Lists migrations and flags archives older than 6 months |
| `python <script> verify --all` | Checks every migration and repairs the session links if something reverted them |
| `python <script> undo <new path>` | Moves the sessions back and restores the original's name (the copy is kept) |
| `python <script> archive <new path>` | Retries renaming the original if it was in use |
| `python <script> relink <old path> <new path>` | Re-applies only the session reassociation |

Reports, the migration registry and progress files live in `%LOCALAPPDATA%\cc-move` (each migration has a
`report.md`), so they survive plugin updates and uninstalls. Backups of Claude Code's state go to
`~/.claude/backups/cc-move/`.

## Troubleshooting

| Message | What to do |
|---|---|
| `There are open Claude Code sessions in the project` | Close every Claude Code session whose folder is inside the project, then try again. |
| `First close the VS Code window of this project` | Close that window (only that one) and try again. |
| `The current directory is inside the source` | Start Claude Code in another folder, outside the project. |
| `The destination already exists and is not an empty folder` | Choose a path that does not exist yet (an empty folder is fine). |
| `Could not rename ...` (archiving) | Something still uses the original folder (a terminal, an editor, an antivirus scan). Close it and run `archive`. |
| `The copy does NOT match the source` | Nothing in Claude Code was changed. Look at `manifest-diff.txt` in the report folder; often a file changed during the copy. Try again with the project closed. |
| `cc-move only works on Windows` / `needs Python 3.8` | See the requirements. If `python` is not found but `py` is, Claude uses `py -3`. |

## Update and uninstall

```
claude plugin marketplace update cc-move
claude plugin update cc-move@cc-move
claude plugin uninstall cc-move@cc-move
```

Uninstalling keeps your reports and the migration registry in `%LOCALAPPDATA%\cc-move`.

## How is this different from `/cd`?

`/cd <path>` moves the **current session** to another directory. It does not move the folder, the other sessions,
the project settings or the VS Code tabs. cc-move moves the folder and everything Claude Code keeps about it.

## Caveats

- Claude Code's and VS Code's storage formats are internal and undocumented; they may change. Each release states
  the Claude Code version it was tested with.
- Moves between folders on the same machine only. Multi-root workspaces (`.code-workspace` files) are not handled.
- Users have reported that an open session can rewrite `~/.claude.json` from memory when it closes. This was not
  reproduced while testing, but `verify --all` repairs it if it happens.

## Development

```
python -m unittest discover -s tests -v     # tests use a synthetic ~/.claude and %APPDATA%
claude --plugin-dir ./plugin                 # try the plugin locally
claude plugin validate ./plugin              # check the manifest
```

See [`CLAUDE.md`](CLAUDE.md) for the layout and the verified storage details. Issues and pull requests are welcome.

## License

[MIT](LICENSE)
