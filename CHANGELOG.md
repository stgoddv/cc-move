# Changelog

## 0.1.0 — unreleased

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
