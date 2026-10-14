"""PreToolUse hook: Claude Code always asks the user before a cc-move command that changes something.

Without it, auto mode may approve `migrate` on its own. The hook answers "ask", which prompts in every mode.
"""

import json
import re
import sys

CHANGING_COMMANDS = ("migrate", "undo", "archive", "relink", "prune")
PREVIEWABLE = ("migrate", "prune")  # with --dry-run they change nothing
COMMAND_WORD = re.compile(r"(?<![\w\\/.-])(" + "|".join(CHANGING_COMMANDS) + r")(?![\w\\/.-])")
REASON = "cc-move: this command moves folders or changes Claude Code's state, so it always needs your approval."


def needs_approval(command):
    """True if `command` runs cc_move.py with a subcommand that changes files or Claude Code's state."""
    _, found, rest = command.partition("cc_move.py")
    words = COMMAND_WORD.findall(rest) if found else []
    if not words:
        return False
    return not (words[0] in PREVIEWABLE and "--dry-run" in rest)


def main():
    """Reads the tool call from stdin and prints an "ask" decision when it needs approval."""
    try:
        command = json.load(sys.stdin).get("tool_input", {}).get("command", "")
    except (ValueError, AttributeError):
        return 0
    if needs_approval(command):
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": REASON,
        }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
