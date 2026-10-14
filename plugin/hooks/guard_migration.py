"""PreToolUse hook registered by /cc-move:migrate: no commits, pushes or deploys for the rest of that session.

The checks used to be `if` rules such as `Bash(git push *)` with a hook that always blocked. Claude Code runs a hook
anyway when the command contains a $VARIABLE, $(...) or backticks, so harmless commands like
`du -sh "$LOCALAPPDATA/cc-move"` were blocked too. Here the script reads the command and decides by itself.
"""

import json
import re
import sys

ARG = r"(?:\"[^\"]*\"|'[^']*'|\S+)"
GIT = r"git(?:\.exe)?\s+(?:-[Cc]\s+" + ARG + r"\s+|--?[\w-]+(?:=" + ARG + r")?\s+)*"
NO_COMMITS = "no commits in a session that used /cc-move:migrate. Open a new session to commit."
NO_PUSHES = "no pushes in a session that used /cc-move:migrate. Open a new session to push."
NO_DEPLOYS = "no deploys in a session that used /cc-move:migrate."
RULES = [
    (GIT + r"commit\b", NO_COMMITS),
    (GIT + r"push\b", NO_PUSHES),
    (r"npm(?:\.cmd)?\s+publish\b", NO_DEPLOYS),
    (r"terraform\s+apply\b", NO_DEPLOYS),
    (r"cdk\s+deploy\b", NO_DEPLOYS),
    (r"(?:serverless|sls)\s+deploy\b", NO_DEPLOYS),
    (r"vercel\b", NO_DEPLOYS),
    (r"firebase\s+deploy\b", NO_DEPLOYS),
]
# Where a new command can start: after && || ; | & newlines, inside $( ... ), ( ... ) or backticks.
SEPARATORS = re.compile(r"&&|\|\||[;|&\n`(]|\$\(")
ENV_PREFIX = re.compile(r"^(?:\w+=" + ARG + r"\s+)*")


def blocked_reason(command):
    """Why `command` must not run during a migration (None if it may)."""
    for part in SEPARATORS.split(command):
        words = ENV_PREFIX.sub("", part.strip())
        for pattern, reason in RULES:
            if re.match(pattern, words, re.IGNORECASE):
                return reason
    return None


def main():
    """Reads the tool call from stdin; exits with 2 (blocked) and a message when the command is not allowed."""
    try:
        command = json.load(sys.stdin).get("tool_input", {}).get("command", "")
    except (ValueError, AttributeError):
        return 0
    reason = blocked_reason(command) if isinstance(command, str) else None
    if reason:
        sys.stderr.write(f"Blocked by cc-move: {reason}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
