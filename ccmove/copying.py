"""Scanning, hash manifest, copying with robocopy and recreating links."""

import hashlib
import os
import queue
import re
import stat
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import _winapi

from . import Abort
from .paths import full, is_under, long_path, strip_long_prefix

# Dependency folders: by default they are not copied and are recreated in phase 2 (--with-deps copies them).
# Venvs are detected by their pyvenv.cfg, not by name.
NODE_MODULES = "node_modules"
TAG_JUNCTION = 0xA0000003
TAG_SYMLINK = 0xA000000C
HASH_THREADS = 8
ROOT = "<ROOT>"  # in the manifest, internal links point to <ROOT>/... to compare source and destination
FILE_LINE = re.compile(r"^\t[^\t]*\t\t\s*(\d+)\t")  # robocopy /BYTES file line: <tab>label<tab><tab>size<tab>path


class Tree:
    """Contents of a folder as it is copied and compared."""

    def __init__(self, root, with_deps):
        self.root = full(root)
        self.with_deps = with_deps  # whether dependency folders are walked (and copied) or skipped
        self.dirs = []    # rel
        self.files = []   # (rel, long path, size)
        self.links = []   # (rel, kind, real target, normalized target)
        self.deps = []    # (rel, kind): top-level dependency folders
        self.bytes = 0

    def venv_dirs(self):
        """Relative paths of the virtual environments found."""
        return [rel for rel, kind in self.deps if kind == "venv"]


def scan(root, with_deps=False, heartbeat=None):
    """Walks `root` without following links; enters dependency folders only if `with_deps`.
    `heartbeat(files_so_far)` is called after each file, so large scans can show they are alive."""
    tree = Tree(root, with_deps)
    pending = [("", long_path(root), False)]  # (rel, path, is it inside a dependency?)
    while pending:
        rel_dir, path, inside_dep = pending.pop()
        with os.scandir(path) as it:
            entries = list(it)
        for entry in entries:
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            st = entry.stat(follow_symlinks=False)
            if st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                kind = _link_kind(entry, st)
                target = strip_long_prefix(os.readlink(entry.path))
                tree.links.append((rel, kind, target, _normalize_target(target, tree.root)))
            elif entry.is_dir(follow_symlinks=False):
                dep_kind = None if inside_dep else dependency_kind(entry.name, entry.path)
                if dep_kind:
                    tree.deps.append((rel, dep_kind))
                    if not with_deps:
                        continue
                tree.dirs.append(rel)
                pending.append((rel, entry.path, inside_dep or bool(dep_kind)))
            else:
                tree.files.append((rel, entry.path, st.st_size))
                tree.bytes += st.st_size
                if heartbeat:
                    heartbeat(len(tree.files))
    return tree


def dependency_kind(name, path):
    """'node_modules' or 'venv' if the folder holds dependencies; None otherwise."""
    if name.lower() == NODE_MODULES:
        return NODE_MODULES
    if os.path.isfile(os.path.join(path, "pyvenv.cfg")):
        return "venv"
    return None


def dir_stats(path):
    """(files, bytes) of a folder without following links. To report the weight of dependencies."""
    files = size = 0
    pending = [long_path(path)]
    while pending:
        with os.scandir(pending.pop()) as it:
            for entry in it:
                st = entry.stat(follow_symlinks=False)
                if st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(entry.path)
                else:
                    files += 1
                    size += st.st_size
    return files, size


def _link_kind(entry, st):
    """'junction', 'symlink' or 'symlinkd'; aborts on other reparse points."""
    tag = getattr(st, "st_reparse_tag", 0)
    if tag == TAG_JUNCTION:
        return "junction"
    if tag == TAG_SYMLINK:
        return "symlinkd" if st.st_file_attributes & stat.FILE_ATTRIBUTE_DIRECTORY else "symlink"
    raise Abort(f"Unknown reparse point (tag {tag:#x}) at {strip_long_prefix(entry.path)}; "
                "I do not know how to copy it safely.")


def _normalize_target(target, root):
    """Target of a link relative to the root if it points inside the project."""
    if os.path.isabs(target) and is_under(target, root):
        rel = os.path.relpath(target, root)
        return ROOT if rel == "." else f"{ROOT}/" + rel.replace("\\", "/")
    return target


def hash_files(tree, progress, stage):
    """SHA-256 of each file, in parallel, reporting to `progress` as the `stage` stage."""
    hashes = {}
    progress.start_stage(stage, tree.bytes)
    with ThreadPoolExecutor(HASH_THREADS) as pool:
        digests = pool.map(lambda f: _sha256(f[1]), tree.files)
        for (rel, _, size), digest in zip(tree.files, digests):
            hashes[rel] = digest
            progress.advance(size)
    return hashes


def _sha256(path):
    """SHA-256 of a file, reading 1 MB at a time."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def manifest(tree, hashes):
    """Sorted lines that describe the tree: D folder, F file size hash, L link kind target."""
    lines = [f"D\t{rel}" for rel in tree.dirs]
    lines += [f"F\t{rel}\t{size}\t{hashes[rel]}" for rel, _, size in tree.files]
    lines += [f"L\t{rel}\t{kind}\t{norm}" for rel, kind, _, norm in tree.links]
    return sorted(lines)


def manifest_hash(lines):
    """A single hash that summarizes the whole manifest."""
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def compare_manifests(src_lines, dst_lines, skipped_links=()):
    """(missing or different in the destination, extra in the destination), ignoring links that were not created."""
    skip = tuple(f"L\t{rel}\t" for rel in skipped_links)
    expected = {line for line in src_lines if not line.startswith(skip)} if skip else set(src_lines)
    actual = set(dst_lines)
    return sorted(expected - actual), sorted(actual - expected)


def write_lines(path, lines):
    """Saves a list of lines as a UTF-8 text file."""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))


def robocopy(tree, dst, log_path, progress):
    """Copies the scanned tree to `dst`: without links (recreated separately) and, unless --with-deps, without dependencies.

    robocopy prints one line per file (/TEE /BYTES); reading them live gives the bytes copied so far.
    """
    args = ["robocopy", tree.root, dst, "/E", "/COPY:DAT", "/DCOPY:DAT", "/XJ", "/R:2", "/W:2", "/MT:16",
            "/NP", "/NDL", "/NJH", "/BYTES", "/TEE", f"/UNILOG:{log_path}"]
    if not tree.with_deps:
        args += ["/XD", NODE_MODULES] + [os.path.join(tree.root, rel.replace("/", "\\")) for rel in tree.venv_dirs()]
    progress.start_stage("copying", tree.bytes)
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    lines = queue.Queue()
    threading.Thread(target=_read_lines, args=(process.stdout, lines), daemon=True).start()
    while True:
        try:
            line = lines.get(timeout=1)
        except queue.Empty:
            progress.heartbeat()  # a single huge file can take minutes without new lines
            continue
        if line is None:  # end of output
            break
        progress.advance(copied_bytes(line))
    code = process.wait()
    if code >= 8:  # robocopy uses 0-7 for success
        raise Abort(f"robocopy failed (code {code}); see {log_path}")


def _read_lines(stream, lines):
    """Moves robocopy's output lines into a queue, then None to mark the end.
    robocopy writes in the console's OEM code page."""
    for raw in stream:
        lines.put(raw.decode("oem", "replace"))
    lines.put(None)


def copied_bytes(line):
    """Size of the file in a robocopy file line ('<tab>label<tab><tab>  123456<tab>path'), 0 for other lines.
    Does not depend on the label, which robocopy translates to the Windows language."""
    m = FILE_LINE.match(line)
    return int(m.group(1)) if m else 0


def recreate_links(tree, dst):
    """Creates the source's links in `dst`; those that pointed inside the project are repointed to the destination."""
    created, failed = [], []
    for rel, kind, target, norm in tree.links:
        link = long_path(os.path.join(dst, rel.replace("/", "\\")))
        new_target = full(dst + norm[len(ROOT):].replace("/", "\\")) if norm.startswith(ROOT) else target
        try:
            if kind == "junction":
                _winapi.CreateJunction(new_target, link)
            else:  # creating symlinks may require developer mode or administrator
                os.symlink(new_target, link, target_is_directory=(kind == "symlinkd"))
            created.append({"path": rel, "kind": kind, "target": new_target, "remapped": new_target != target})
        except OSError as e:
            failed.append({"path": rel, "kind": kind, "target": new_target, "error": str(e)})
    return created, failed
