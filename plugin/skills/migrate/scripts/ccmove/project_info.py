"""Project information that phase 2 needs: git, virtual environments, lockfiles, written paths."""

import os
import re
import subprocess

from .copying import dependency_kind
from .paths import list_dir, long_path

LOCKFILES = {
    "pnpm-lock.yaml", "package-lock.json", "yarn.lock", "bun.lock", "bun.lockb",
    "poetry.lock", "uv.lock", "Pipfile.lock", "pyproject.toml", "requirements.txt", "requirements-dev.txt",
}
MAX_TEXT_FILE = 5_000_000
# Build outputs and caches: the next build regenerates them, so old paths inside them are noise in the report.
BUILD_DIRS = {
    ".cxx", ".gradle", "cdk.out", ".aws-sam", ".next", ".nuxt", ".svelte-kit", ".turbo", ".parcel-cache",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".terraform",
}
# Generic names that are build outputs only when git ignores them (otherwise they may be source code).
MAYBE_BUILD_DIRS = {"build", "dist", "out", "target", "obj", "bin"}


def git_info(path):
    """HEAD, branch, `git status` and worktrees, or None if it is not a git repo.

    Uses --no-optional-locks so .git/index is not modified: the source must not change
    between computing the hashes and copying.
    """
    if not os.path.exists(os.path.join(path, ".git")):
        return None

    def git(*args):
        try:
            r = subprocess.run(["git", "--no-optional-locks", "-C", path, *args], capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=600)
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"(error: {e})"
        return r.stdout.rstrip("\n") if r.returncode == 0 else f"(error: {r.stderr.strip()})"

    return {
        "git_dir_is_file": os.path.isfile(os.path.join(path, ".git")),
        "head": git("rev-parse", "HEAD"),
        "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "status": git("-c", "core.quotepath=off", "status", "--porcelain"),
        "worktrees": git("worktree", "list"),
    }


def lockfiles(tree):
    """Relative paths of copied lockfiles and dependency manifests."""
    return sorted(rel for rel, _, _ in tree.files if rel.rsplit("/", 1)[-1] in LOCKFILES)


def venv_info(project, rel, report_dir):
    """Python version and packages of a venv; saves the list in <report_dir>/venvs/."""
    venv = os.path.join(project, rel.replace("/", "\\"))
    config = _read_pyvenv_cfg(venv)
    packages, method = _pip_freeze(venv)
    if packages is None:
        packages, method = _dist_info_list(venv), "list of *.dist-info"
    freeze_file = "venvs/" + rel.replace("/", "__") + ".freeze.txt"
    os.makedirs(os.path.join(report_dir, "venvs"), exist_ok=True)
    with open(os.path.join(report_dir, freeze_file), "w", encoding="utf-8", newline="\n") as f:
        f.write(packages)
    return {
        "path": rel,
        "python_version": config.get("version") or config.get("version_info"),
        "home": config.get("home"),
        "freeze_file": freeze_file,
        "freeze_method": method,
        "packages": sum(1 for line in packages.splitlines() if line.strip()),
    }


def _read_pyvenv_cfg(venv):
    """Keys of pyvenv.cfg (home, version, ...)."""
    config = {}
    try:
        with open(os.path.join(venv, "pyvenv.cfg"), encoding="utf-8", errors="replace") as f:
            for line in f:
                if "=" in line:
                    key, value = line.split("=", 1)
                    config[key.strip()] = value.strip()
    except OSError:
        pass
    return config


def _pip_freeze(venv):
    """(pip freeze output, method) using the venv's Python, or (None, None) if it does not work."""
    python = os.path.join(venv, "Scripts", "python.exe")
    if not os.path.isfile(python):
        return None, None
    try:
        r = subprocess.run([python, "-m", "pip", "freeze"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    return (r.stdout, "pip freeze") if r.returncode == 0 else (None, None)


def _dist_info_list(venv):
    """Installed packages according to the *.dist-info folders (when the venv's Python does not start)."""
    site_packages = os.path.join(venv, "Lib", "site-packages")
    packages = []
    for name in sorted(list_dir(site_packages)):
        m = re.match(r"^(.+?)-([^-]+)\.dist-info$", name)
        if m:
            packages.append(f"{m.group(1)}=={m.group(2)}")
    return "\n".join(packages) + ("\n" if packages else "")


def stale_refs(project, rewriter):
    """Text files in the project that still mention the old path. Skips .git and dependency folders:
    with --with-deps they can hold hundreds of thousands of files, and phase 2 reviews them anyway.
    Files inside build outputs get a "build" key with that folder, so the report can group them."""
    found = []
    root_path = long_path(project)
    for root, dirs, names in os.walk(root_path):
        dirs[:] = [d for d in dirs if d != ".git" and not dependency_kind(d, os.path.join(root, d))]
        for name in names:
            path = os.path.join(root, name)
            text = _read_small_text(path)
            if text is None:
                continue
            count = rewriter.sub(text)[1]
            if count:
                found.append({"path": os.path.relpath(path, root_path).replace("\\", "/"), "count": count})
    builds = build_outputs(project, [x["path"] for x in found])
    for x in found:
        if builds[x["path"]]:
            x["build"] = builds[x["path"]]
    return found


def build_outputs(project, rels):
    """For each relative path ('a/b/c'), the build output or cache folder it is inside ('a/.cxx'), or None."""
    generic = {p for rel in rels for p in _parent_folders(rel) if _name(p) in MAYBE_BUILD_DIRS}
    ignored = _git_ignored(project, generic)
    return {rel: next((p for p in _parent_folders(rel) if _name(p) in BUILD_DIRS or p in ignored), None)
            for rel in rels}


def _parent_folders(rel):
    """'a/b/c' -> ['a', 'a/b']: the folders that contain it, outermost first."""
    parts = rel.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def _name(rel):
    """Last part of a relative path, in lower case."""
    return rel.rsplit("/", 1)[-1].lower()


def _git_ignored(project, rels):
    """Those of the relative paths `rels` that git ignores (none if it is not a git repo or git fails).
    Read-only (--no-optional-locks): it also runs on the source, which must not change before the copy."""
    if not rels or not os.path.exists(os.path.join(project, ".git")):
        return set()
    try:  # NUL-separated bytes: in text mode Windows would turn each \n into \r\n, and git would not match them
        r = subprocess.run(["git", "--no-optional-locks", "-C", project, "check-ignore", "-z", "--stdin"],
                           input="\0".join(sorted(rels)).encode("utf-8") + b"\0", capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return set(r.stdout.decode("utf-8", "replace").split("\0")) & set(rels)


def _read_small_text(path):
    """Contents of a text file of up to 5 MB, or None if it is large, binary or unreadable."""
    try:
        if os.path.getsize(path) > MAX_TEXT_FILE:
            return None
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if b"\x00" in data[:8192]:
        return None
    return data.decode("utf-8", "replace")
