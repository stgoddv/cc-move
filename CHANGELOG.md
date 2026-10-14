# Changelog

## 0.1.2 — 2026-10-14

- Claude can now start the `migrate` skill when you ask in your own words ("use cc-move to move X to Y"); before,
  only typing `/cc-move:migrate` worked.
- New plugin hook: Claude Code always asks your permission before a cc-move command that changes something
  (`migrate`, `undo`, `archive`, `relink`), in every permission mode, auto mode included. The dry run, `verify` and
  `status` still run without asking.

## 0.1.1 — 2026-10-14

- Paths given with 8.3 short names (`C:\Users\RUNNER~1\...`) are expanded to their long form, which is what Claude
  Code and VS Code record. Before, such a path did not find the VS Code state of the folder (and failed the tests on
  GitHub's Windows runners).
- CI uses the current `actions/checkout` and `actions/setup-python`.

## 0.1.0 — 2026-10-14

First version, tested on Windows 11 with Claude Code 2.1.299 and its VS Code extension.

- `/cc-move:migrate <source> <destination>`: two-phase migration of a project folder.
  - Phase 1 (script): copy with robocopy, verify every file by SHA-256, move and rewrite the Claude Code session
    folders, `~/.claude.json` and the prompt history, restore the VS Code state of the folder (open Claude tabs),
    archive the original as `archive-<date>-<name>`, and leave a note in `CLAUDE.local.md`.
  - Phase 2 (agent): audit and recreate what a copy cannot carry over (dependencies, absolute paths), without
    commits or deploys.
- `node_modules` and virtual environments are skipped by default; `--with-deps` copies them.
- Time-based progress lines (at most one per minute) and a `progress.txt` file.
- `verify`, `relink`, `archive`, `undo`, `phase2` and `status` commands.
