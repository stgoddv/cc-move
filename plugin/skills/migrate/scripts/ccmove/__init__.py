"""cc-move: moves a repo to another folder without losing its Claude Code sessions."""


class Abort(Exception):
    """Expected error: reported to the user and the command ends without a traceback."""


def log(msg=""):
    print(msg, flush=True)
