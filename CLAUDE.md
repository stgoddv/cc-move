# cc-move — development guide

cc-move is a Claude Code plugin that moves a project folder to a new path without losing its Claude Code
sessions. Claude Code ties sessions to the folder by its path, so moving a folder by hand orphans them.
The user-facing procedure lives in `plugin/skills/migrate/SKILL.md`; this file is for working on the code.

## Layout

```
.claude-plugin/marketplace.json        marketplace "cc-move" (source: ./plugin)
plugin/.claude-plugin/plugin.json      plugin manifest (keep "version" in sync with CHANGELOG.md and git tags)
plugin/skills/migrate/SKILL.md         /cc-move:migrate — the two-phase procedure the agent follows
plugin/skills/migrate/scripts/         cc_move.py (entry point) and the ccmove package (phase 1)
plugin/agents/auditor.md               read-only phase 2 auditor subagent
plugin/hooks/                          hooks.json + confirm_changes.py (always ask before migrate/undo/archive/relink/prune);
                                       guard_migration.py (the skill's no-commit/no-deploy hook)
tests/test_cc_move.py                  end-to-end tests on a synthetic project, ~/.claude and %APPDATA%
```

## Commands

- Tests: `python -m unittest discover -s tests -v` (never touch the real `~/.claude`: tests pass
  `--claude-home`, `--state-dir` and a fake `APPDATA`).
- Try the plugin locally: `claude --plugin-dir ./plugin`
- Validate: `claude plugin validate ./plugin` and `claude plugin validate .` (marketplace).
- Run the script directly: `python plugin/skills/migrate/scripts/cc_move.py --help`

## Constraints

- Windows only (robocopy, NTFS junctions, `_winapi`). Python ≥ 3.8, standard library only.
- `cc_move.py` checks the platform before importing `ccmove`, so other systems get a clear error.
- State (registry, reports) lives in `%LOCALAPPDATA%\cc-move`, outside the plugin folder: plugin updates and
  uninstalls must not lose it.
- Everything about Claude Code and VS Code storage is an internal, undocumented format. Prefer best-effort steps
  that warn over steps that abort, except before the copy is verified.

## Style

- Simple, readable code; SOLID/DRY without dogma.
- Every function has a clear name and a one-line docstring saying what it does or why it exists.
- User-facing text in English. Commit after each coherent change.

## Modules (`plugin/skills/migrate/scripts/ccmove/`)

| Module | Responsibility |
|---|---|
| `cli.py` | commands and arguments |
| `migrate.py` | phase 1 step by step (`Migration` class) |
| `rename.py` | case-only rename in place (`CaseRename`, a `Migration` without copy, archive or note) |
| `copying.py` | scan, SHA-256 manifest, robocopy, links |
| `progress.py` | time-based progress lines and `progress.txt` |
| `claude_state.py` | session folders, `~/.claude.json`, history, reassociation, open sessions and windows |
| `rewriter.py` | replaces the old path with the new one in every form |
| `editor_state.py` | VS Code per-folder state (`workspaceStorage`): open Claude tabs and the rest |
| `project_info.py` | git, venvs, lockfiles and old paths written inside the project |
| `finalize.py` | archiving the original and the `CLAUDE.local.md` note |
| `registry.py` / `report.py` | `migrations.jsonl` (chronological; `save` replaces in place) and the reports |
| `backups.py` | zip backups of Claude Code's state, 30-day expiry, `prune` (also of pre-0.3 folder backups) |
| `paths.py` | Windows path utilities |

## Verified storage details (Windows 11, Claude Code 2.1.299)

- Session folder: `~/.claude/projects/<path with every non-alphanumeric character replaced by ->`. Over 200
  characters Claude Code truncates it and appends a hash; the script refuses such names.
- Paths appear as `c:\\…` (JSON), `/c/…` (Git Bash), `C:/…`, `file:///c%3A/…` (VS Code URIs, spaces as `%20`),
  in UPPERCASE (Python extension) and in the encoded folder-name form.
- `~/.claude.json` has `projects` keys that differ only in the drive letter's case (`C:/…` vs `c:/…`).
- Open sessions: `~/.claude/sessions/<pid>.json` (`cwd`). Open IDE windows with the Claude extension:
  `~/.claude/ide/*.lock` (`pid`, `workspaceFolders`). Lock files of dead processes stay behind; check the pid.
- VS Code keeps per-folder state in `%APPDATA%\Code\User\workspaceStorage\<id>\state.vscdb` (SQLite, table
  `ItemTable`), with `<id>` = md5(path with lowercase drive + creation time in ms). The Claude extension stores
  the open tabs under `Anthropic.claude-code` → `panelTabSessions`.
- Reported by users on GitHub (#86713, #96018) but **not reproduced** here: an open session rewriting
  `~/.claude.json` from memory when it closes. `verify --all` repairs it if it ever happens.
- Case-only rename (verified 2026-10-14): `os.rename('demo', 'Demo')` works on NTFS; Claude Code keeps finding the
  sessions (same session folder, NTFS ignores case) and creates no new `~/.claude.json` key, but VS Code's
  `workspaceStorage` id changes (md5 includes the exact case), so the state must be copied. Every path comparison in
  the code ignores case on purpose; the rewriter matches case-insensitively and counts only replacements that change
  the text. `paths.full` expands 8.3 names with `GetLongPathNameW`, which returns the case on disk: never pass it the
  requested new name of a case-only rename.
- Hooks (documented in hooks.md, verified 2026-10-14): an `if` pattern that names more than the command, such as
  `Bash(git push *)`, runs the hook anyway when the command contains `$VAR`, `$(...)` or backticks. So hooks must
  decide by reading the command (`guard_migration.py`), never block unconditionally behind an `if`.
  `${CLAUDE_PLUGIN_ROOT}` is substituted in SKILL.md frontmatter hook commands. Skill hooks last until the session
  ends; a VS Code window reload ends them.
- Right after closing a VS Code window, renaming its folder can fail for a few seconds ("Access denied");
  `finalize.archive_original` retries.
