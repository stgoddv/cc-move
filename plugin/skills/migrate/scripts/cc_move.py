#!/usr/bin/env python3
"""cc-move entry point: moves a repo without losing its Claude Code sessions.

Usage: python cc_move.py --help        Full procedure: CLAUDE.md
"""

import sys


def environment_problem(platform=sys.platform, version=sys.version_info):
    """Reason why cc-move cannot run here, or None. Checked before importing the package,
    which uses Windows-only modules (_winapi) and would fail with an unclear error."""
    if platform != "win32":
        return "cc-move only works on Windows (it uses robocopy, NTFS junctions and Windows paths)."
    if tuple(version[:2]) < (3, 8):
        return "cc-move needs Python 3.8 or higher."
    return None


if __name__ == "__main__":
    problem = environment_problem()
    if problem:
        print(f"ERROR: {problem}", file=sys.stderr)
        sys.exit(2)

    from ccmove.cli import main

    sys.exit(main())
