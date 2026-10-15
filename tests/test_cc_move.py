"""End-to-end tests of cc-move on a synthetic project and a synthetic ~/.claude.

Run from the repo root:  python -m unittest discover -s tests -v
It does not touch the real ~/.claude: everything lives in a temporary folder.
"""

import ctypes
import datetime as dt
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from urllib.parse import quote

import _winapi

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "plugin", "skills", "migrate", "scripts"))
from ccmove import cli, copying, editor_state, finalize, paths, progress, rewriter  # noqa: E402
from ccmove.registry import Registry  # noqa: E402

UUID = "11111111-2222-3333-4444-555555555555"
OLD_MTIME = 1_700_000_000


def write(path, text="", binary=None):
    """Creates a file (and its folders), accepting long paths."""
    path = paths.long_path(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(binary if binary is not None else text.encode("utf-8"))


def read(path):
    """Contents of a text file, accepting long paths."""
    with open(paths.long_path(path), encoding="utf-8") as f:
        return f.read()


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def git_bash(p):
    """'C:\\a\\b' -> '/c/a/b', as Git Bash writes paths."""
    return "/" + p[0].lower() + p[2:].replace("\\", "/")


def forward(p):
    """'C:\\a\\b' -> 'C:/a/b', like the keys of ~/.claude.json."""
    return p.replace("\\", "/")


def in_json(p):
    """How a path appears inside a JSON text (escaped backslashes)."""
    return json.dumps(p)[1:-1]


class Fixture:
    """Test project with the hard cases and a ~/.claude that references it in every form."""

    def __init__(self, tmp):
        desktop = os.path.join(tmp, "OneDrive", "Desktop")
        self.src = os.path.join(desktop, "Proj")
        self.sibling = os.path.join(desktop, "Proj2")         # must not be touched
        self.prefix_sibling = os.path.join(desktop, "Proj-old")  # shares the encoded prefix
        self.external = os.path.join(tmp, "external")
        self.dst = os.path.join(tmp, "Desktop", "repos", "Proj")
        self.home = os.path.join(tmp, "home")
        self.state_dir = os.path.join(tmp, "state")
        for p in (os.path.dirname(self.dst), self.state_dir, self.sibling, self.prefix_sibling, self.external):
            os.makedirs(p)
        self.claude = os.path.join(self.home, ".claude")
        self.projects = os.path.join(self.claude, "projects")
        self.config = os.path.join(self.home, ".claude.json")
        self.history = os.path.join(self.claude, "history.jsonl")
        self.src_dir_name = paths.project_dir_name(self.src)
        self.dst_dir_name = paths.project_dir_name(self.dst)
        self.appdata = os.path.join(tmp, "appdata")
        self.storage_root = os.path.join(self.appdata, "Code", "User", "workspaceStorage")
        self._make_project()
        self._make_claude_home()
        self._make_editor_state()

    def _make_project(self):
        """Normal files, binary, empty folder, path >260, dependencies, junctions and git repo."""
        s = self.src
        write(os.path.join(s, "a.txt"), "hola\n")
        write(os.path.join(s, "sub", "b.bin"), binary=os.urandom(300_000))
        os.makedirs(os.path.join(s, "empty"))
        deep = os.path.join(s, *(["carpeta-con-nombre-largo-0123456789"] * 8), "profundo.txt")
        assert len(deep) > 260
        self.deep_rel = os.path.relpath(deep, s)
        write(deep, "ruta larga\n")
        write(os.path.join(s, "node_modules", "x", "index.js"), "x\n")
        write(os.path.join(s, "packages", "p", "node_modules", "y", "i.js"), "y\n")
        write(os.path.join(s, "packages", "p", "package.json"), "{}\n")
        write(os.path.join(s, "pnpm-lock.yaml"), "lock\n")
        write(os.path.join(s, ".venv", "pyvenv.cfg"), "home = C:\\Python313\nversion = 3.13.7\n")
        write(os.path.join(s, ".venv", "Lib", "site-packages", "foo-1.2.dist-info", "METADATA"), "m\n")
        write(os.path.join(s, "config.txt"), f"data_dir = {s}\\data\n")
        # Like pnpm: junction inside node_modules to a package of the repo itself.
        _winapi.CreateJunction(os.path.join(s, "packages", "p"), os.path.join(s, "node_modules", "p"))
        _winapi.CreateJunction(os.path.join(s, "sub"), os.path.join(s, "link_internal"))
        _winapi.CreateJunction(self.external, os.path.join(s, "link_external"))
        write(os.path.join(s, ".gitignore"), "node_modules/\n.venv/\n")
        git = ["git", "-C", s, "-c", "core.autocrlf=false", "-c", "user.email=t@t", "-c", "user.name=t"]
        subprocess.run(git + ["init", "-q"], check=True)
        subprocess.run(git + ["add", "a.txt", ".gitignore"], check=True)
        subprocess.run(git + ["commit", "-qm", "init"], check=True)

    def _make_claude_home(self):
        """Transcripts, subagents, tool-results, memory, ~/.claude.json and history."""
        s, sib = self.src, self.sibling
        proj = os.path.join(self.projects, self.src_dir_name)
        lines = [
            {"type": "user", "cwd": s, "text": f"lee {s}\\a.txt y {sib}\\x.txt, también {git_bash(s)}/sub "
                                               f"y {forward(s)}/a.txt."},
            {"type": "tool", "nested": json.dumps({"path": s + "\\a.txt"}), "slug": self.src_dir_name + "/memory"},
            {"type": "user", "cwd": git_bash(s), "text": f"fin de {s}. y {s}.bak queda igual"},
        ]
        self.transcript_name = UUID + ".jsonl"
        transcript = os.path.join(proj, self.transcript_name)
        write(transcript, "".join(json.dumps(x) + "\n" for x in lines))
        os.utime(transcript, (OLD_MTIME, OLD_MTIME))
        write(os.path.join(proj, UUID, "subagents", "agent-1.jsonl"), json.dumps({"cwd": s}) + "\n")
        write(os.path.join(proj, UUID, "tool-results", "t.txt"), f"salida en {s}\\sub\n")
        write(os.path.join(proj, "memory", "MEMORY.md"), f"- proyecto en `{s}`\n")
        self._session_dir(self.src_dir_name + "-sub", s + "\\sub")  # sessions of a subfolder
        self._session_dir(paths.project_dir_name(sib), sib)
        self._session_dir(paths.project_dir_name(self.prefix_sibling), self.prefix_sibling)

        config = {"numStartups": 3, "projects": {
            forward(s)[0].upper() + forward(s)[1:]: {"allowedTools": ["A"], "hasTrustDialogAccepted": True},
            forward(s)[0].lower() + forward(s)[1:]: {"allowedTools": [], "hasTrustDialogAccepted": True},
            forward(s) + "/sub": {"allowedTools": []},
            forward(sib): {"allowedTools": ["B"]},
        }, "githubRepoPaths": {"u/proj": [s], "u/other": [sib]}}
        write(self.config, json.dumps(config, indent=2))
        history = [{"display": "x", "project": s}, {"display": "y", "project": sib}]
        write(self.history, "".join(json.dumps(h) + "\n" for h in history))
        os.makedirs(os.path.join(self.claude, "sessions"))

    def _make_editor_state(self):
        """VS Code state of the old folder: Claude tab, an open editor and Python data."""
        self.old_storage = os.path.join(self.storage_root, "old-state")
        uri = editor_state.folder_uri(self.src)
        write(os.path.join(self.old_storage, "workspace.json"), json.dumps({"folder": uri}))
        write(os.path.join(self.old_storage, "chatSessions", "c.jsonl"), json.dumps({"cwd": self.src}) + "\n")
        rows = [
            (editor_state.CLAUDE_EXTENSION_KEY,
             json.dumps({"panelTabSessions": [{"sessionId": UUID, "title": "Mi sesión"}], "log": self.src})),
            ("memento/workbench.editors.files.textFileEditor",
             json.dumps([[uri + "/a.txt", {}], [uri + "/a.txt?" + quote(json.dumps({"path": self.src}), safe=""), {}]])),
            ("ms-python.python", json.dumps({self.src.upper(): True}).encode("utf-8")),  # BLOB, in upper case
        ]
        con = sqlite3.connect(os.path.join(self.old_storage, "state.vscdb"))
        con.execute("CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
        con.executemany("INSERT INTO ItemTable VALUES (?, ?)", rows)
        con.commit()
        con.close()

    def editor_item(self, storage, key):
        """Value (text) of a key in the state.vscdb of a state folder."""
        con = sqlite3.connect(os.path.join(storage, "state.vscdb"))
        value = con.execute("SELECT value FROM ItemTable WHERE key = ?", (key,)).fetchone()[0]
        con.close()
        return value if isinstance(value, str) else value.decode("utf-8")

    def _session_dir(self, name, cwd):
        """Session folder with a transcript whose cwd is `cwd`."""
        write(os.path.join(self.projects, name, "s.jsonl"), json.dumps({"cwd": cwd}) + "\n")

    def run(self, *args):
        """Runs cc-move with this fake ~/.claude; stores the output in self.output."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(["--claude-home", self.home, "--state-dir", self.state_dir, *args])
        self.output = buf.getvalue()
        return code

    def record(self):
        return Registry(self.state_dir).all()[-1]


class FixtureTestCase(unittest.TestCase):
    """Creates a new Fixture in a temporary folder for each test."""

    def setUp(self):
        self.tmp = paths.full(tempfile.mkdtemp(prefix="ccmove-"))  # long names, as Claude Code records them
        self.f = Fixture(self.tmp)
        self.real_appdata = os.environ.get("APPDATA")
        os.environ["APPDATA"] = self.f.appdata  # the VS Code state is fake too

    def tearDown(self):
        if self.real_appdata is not None:
            os.environ["APPDATA"] = self.real_appdata
        shutil.rmtree(paths.long_path(self.tmp), ignore_errors=True)


class RewriterTest(unittest.TestCase):
    def test_replaces_every_form_of_the_path(self):
        rw = rewriter.PathRewriter(r"C:\U\OneDrive\Desktop\Proj", r"C:\U\Desktop\repos\Proj")
        cases = {
            r"C:\U\OneDrive\Desktop\Proj": r"C:\U\Desktop\repos\Proj",
            r"c:\U\OneDrive\Desktop\Proj\a.txt": r"c:\U\Desktop\repos\Proj\a.txt",
            r"C:\\U\\OneDrive\\Desktop\\Proj\\a": r"C:\\U\\Desktop\\repos\\Proj\\a",
            "C:/U/OneDrive/Desktop/Proj/a": "C:/U/Desktop/repos/Proj/a",
            "cd /c/U/OneDrive/Desktop/Proj && ls": "cd /c/U/Desktop/repos/Proj && ls",
            "c--U-OneDrive-Desktop-Proj/x": "c--U-Desktop-repos-Proj/x",
            r"fin C:\U\OneDrive\Desktop\Proj.": r"fin C:\U\Desktop\repos\Proj.",
        }
        for before, after in cases.items():
            self.assertEqual(rw.sub(before)[0], after, before)

    def test_vscode_uri_and_uppercase_forms(self):
        rw = rewriter.PathRewriter(r"C:\U\OneDrive\Desktop\A B", r"C:\U\Desktop\repos\A B")
        self.assertEqual(rw.sub("file:///c%3A/U/OneDrive/Desktop/A%20B/x.md")[0],
                         "file:///c%3A/U/Desktop/repos/A%20B/x.md")
        self.assertEqual(rw.sub(r'{"C:\\U\\ONEDRIVE\\DESKTOP\\A B":1}')[0], r'{"C:\\U\\DESKTOP\\REPOS\\A B":1}')

    def test_uri_encoded_backslashes(self):
        rw = rewriter.PathRewriter(r"C:\U\OneDrive\Desktop\Proj", r"C:\U\Desktop\repos\Proj")
        cases = {  # JSON inside a URI query (VS Code's open editors), single backslashes, lower case, %2F
            "x.md?%7B%22path%22%3A%22c%3A%5C%5CU%5C%5COneDrive%5C%5CDesktop%5C%5CProj%5C%5Ca.md%22%7D":
                "x.md?%7B%22path%22%3A%22c%3A%5C%5CU%5C%5CDesktop%5C%5Crepos%5C%5CProj%5C%5Ca.md%22%7D",
            "c%3A%5CU%5COneDrive%5CDesktop%5CProj%22": "c%3A%5CU%5CDesktop%5Crepos%5CProj%22",
            "c%3a%5cU%5cOneDrive%5cDesktop%5cProj": "c%3a%5cU%5cDesktop%5crepos%5cProj",
            "C%3A%2FU%2FOneDrive%2FDesktop%2FProj%2Fa": "C%3A%2FU%2FDesktop%2Frepos%2FProj%2Fa",
        }
        for before, after in cases.items():
            self.assertEqual(rw.sub(before), (after, 1), before)
        for text in ("c%3A%5C%5CU%5C%5COneDrive%5C%5CDesktop%5C%5CProj2", "c%3A%5CU%5COneDrive%5CDesktop%5CProj%20old",
                     "c%3A%2FU%2FOneDrive%2FDesktop%2FProj-old", "file:///c%3A/U/OneDrive/Desktop/Proj%20old/x"):
            self.assertEqual(rw.sub(text), (text, 0), text)

    def test_case_only_rename_keeps_the_case_of_uri_escapes(self):
        rw = rewriter.PathRewriter(r"C:\U\Desktop\Proj", r"C:\U\Desktop\proj")
        self.assertEqual(rw.sub("c%3a%5cU%5cDesktop%5cProj"), ("c%3a%5cU%5cDesktop%5cproj", 1))
        self.assertEqual(rw.sub("c%3a%5cU%5cDesktop%5cproj"), ("c%3a%5cU%5cDesktop%5cproj", 0))

    def test_case_only_rename_changes_every_form_and_counts_only_real_changes(self):
        rw = rewriter.PathRewriter(r"C:\U\Desktop\Proj", r"C:\U\Desktop\proj")
        cases = {
            r"C:\U\Desktop\Proj\a": r"C:\U\Desktop\proj\a",
            r"c:\\U\\Desktop\\Proj\\a": r"c:\\U\\Desktop\\proj\\a",
            "C:/U/Desktop/Proj/a": "C:/U/Desktop/proj/a",
            "cd /c/U/Desktop/Proj && ls": "cd /c/U/Desktop/proj && ls",
            "file:///c%3A/U/Desktop/Proj/x.md": "file:///c%3A/U/Desktop/proj/x.md",
            "C--U-Desktop-Proj/memory": "C--U-Desktop-proj/memory",
        }
        for before, after in cases.items():
            self.assertEqual(rw.sub(before), (after, 1), before)
        # The new path matches too (case is ignored), but replacing it with itself is not a change.
        for text in (r"C:\U\Desktop\proj\a", r"C:\U\DESKTOP\PROJ", r"C:\U\Desktop\Proj2", r"C:\U\Desktop\Proj-old",
                     "C--U-Desktop-Proj-old"):
            self.assertEqual(rw.sub(text), (text, 0), text)

    def test_leaves_similar_paths_alone(self):
        rw = rewriter.PathRewriter(r"C:\U\OneDrive\Desktop\Proj", r"C:\U\Desktop\repos\Proj")
        for text in (r"C:\U\OneDrive\Desktop\Proj2", r"C:\U\OneDrive\Desktop\Proj-old\x",
                     r"C:\U\OneDrive\Desktop\Proj.bak", "c--U-OneDrive-Desktop-Proj-old",
                     r"D:\U\OneDrive\Desktop\Proj"):
            self.assertEqual(rw.sub(text), (text, 0), text)


class SafetyTest(FixtureTestCase):
    def test_dry_run_changes_nothing(self):
        f = self.f
        self.assertEqual(f.run("migrate", f.src, f.dst, "--dry-run"), 0, f.output)
        self.assertFalse(os.path.exists(f.dst))
        self.assertTrue(os.path.isdir(os.path.join(f.projects, f.src_dir_name)))
        self.assertIn("node_modules", f.output)

    def test_dry_run_flag_can_go_before_the_paths(self):
        f = self.f  # allows authorizing only "migrate --dry-run *" in allowed-tools
        self.assertEqual(f.run("migrate", "--dry-run", f.src, f.dst), 0, f.output)
        self.assertFalse(os.path.exists(f.dst))

    def test_progress_lines_appear_when_the_interval_is_short(self):
        f = self.f
        self.assertEqual(f.run("migrate", "--progress-every", "0", f.src, f.dst), 0, f.output)
        self.assertIn("% | hashing the source", f.output)
        self.assertIn("% | hashing the copy", f.output)

    def test_open_claude_session_blocks_migration(self):
        f = self.f
        write(os.path.join(f.claude, "sessions", "1.json"), json.dumps({"pid": os.getpid(), "cwd": f.src}))
        self.assertEqual(f.run("migrate", f.src, f.dst), 2)
        self.assertIn("open Claude Code sessions", f.output)
        self.assertFalse(os.path.exists(f.dst))

    def test_open_editor_window_blocks_migration(self):
        f = self.f
        lock = {"pid": os.getpid(), "workspaceFolders": [f.src], "ideName": "Visual Studio Code"}
        write(os.path.join(f.claude, "ide", "1.lock"), json.dumps(lock))
        self.assertEqual(f.run("migrate", f.src, f.dst), 2)
        self.assertIn("VS Code window", f.output)
        self.assertFalse(os.path.exists(f.dst))

    def test_existing_editor_state_is_not_overwritten(self):
        f = self.f
        existing = os.path.join(f.storage_root, "already-opened")
        write(os.path.join(existing, "marker.txt"), "estado propio")
        real_workspace_id = editor_state.workspace_id
        editor_state.workspace_id = lambda path: "already-opened"
        try:
            self.assertEqual(f.run("migrate", f.src, f.dst), 0, f.output)
        finally:
            editor_state.workspace_id = real_workspace_id
        self.assertEqual(os.listdir(existing), ["marker.txt"])
        report = read_json(os.path.join(f.record()["report_dir"], "report.json"))
        self.assertTrue(report["editor_state"][0]["status"].startswith("skipped"))

    def test_bad_copy_aborts_before_touching_claude(self):
        f = self.f
        real_robocopy = copying.robocopy

        def robocopy_then_corrupt(tree, dst, log_path, progress):
            real_robocopy(tree, dst, log_path, progress)
            write(os.path.join(dst, "a.txt"), "alterado\n")

        copying.robocopy = robocopy_then_corrupt
        try:
            self.assertEqual(f.run("migrate", f.src, f.dst), 2)
        finally:
            copying.robocopy = real_robocopy
        self.assertIn("does NOT match", f.output)
        self.assertTrue(os.path.isdir(f.src))
        self.assertTrue(os.path.isdir(os.path.join(f.projects, f.src_dir_name)))
        self.assertEqual(f.record()["phase1"], "failed")


class RenameTest(FixtureTestCase):
    """Moving and renaming at once (Proj -> my-proj) carries everything to the new name."""

    def test_move_and_rename(self):
        f = self.f
        new = os.path.join(os.path.dirname(f.dst), "my-proj")
        self.assertEqual(f.run("migrate", f.src, new), 0, f.output)
        text = read(os.path.join(f.projects, paths.project_dir_name(new), f.transcript_name))
        self.assertNotIn(in_json(f.src) + "\\\\", text)
        for present in (in_json(new), git_bash(new), in_json(f.sibling), "Proj.bak"):
            self.assertIn(present, text)
        self.assertFalse(os.path.exists(os.path.join(f.projects, f.src_dir_name)))
        self.assertIn(forward(new), read_json(f.config)["projects"])
        with open(f.history, encoding="utf-8") as fh:
            self.assertEqual([json.loads(line)["project"] for line in fh], [new, f.sibling])
        storage = os.path.join(f.storage_root, editor_state.workspace_id(new))
        self.assertEqual(read_json(os.path.join(storage, "workspace.json")), {"folder": editor_state.folder_uri(new)})
        archive = os.path.join(os.path.dirname(f.src), f"archive-{dt.date.today().isoformat()}-Proj")
        self.assertTrue(os.path.isdir(archive))
        self.assertEqual(f.run("verify", new), 0, f.output)


class CaseRenameTest(FixtureTestCase):
    """A change of case alone (Proj -> proj) renames the folder in place and relinks everything to the new case."""

    def setUp(self):
        super().setUp()
        self.parent = os.path.dirname(self.f.src)
        self.new = os.path.join(self.parent, "proj")

    def rename(self):
        self.assertEqual(self.f.run("migrate", self.f.src, self.new), 0, self.f.output)

    def test_folder_and_sessions_get_the_new_case(self):
        f = self.f
        self.rename()
        self.assertIn("proj", os.listdir(self.parent))
        self.assertNotIn("Proj", os.listdir(self.parent))
        self.assertEqual(read(os.path.join(self.new, "a.txt")), "hola\n")
        new_dir_name = paths.project_dir_name(self.new)
        dirs = os.listdir(f.projects)
        self.assertTrue({new_dir_name, new_dir_name + "-sub"} <= set(dirs), dirs)
        self.assertNotIn(f.src_dir_name, dirs)
        transcript = os.path.join(f.projects, new_dir_name, f.transcript_name)
        text = read(transcript)
        self.assertNotIn(in_json(f.src) + "\\\\", text)
        for present in (in_json(self.new) + "\\\\a.txt", git_bash(self.new) + "/sub", forward(self.new) + "/a.txt",
                        in_json(f.sibling), "Proj.bak", new_dir_name + "/memory"):
            self.assertIn(present, text)
        self.assertEqual(int(os.stat(transcript).st_mtime), OLD_MTIME)
        self.assertIn(in_json(self.new), read(os.path.join(f.projects, new_dir_name, UUID, "subagents", "agent-1.jsonl")))
        self.assertIn("Rename complete", f.output)

    def test_config_and_history_get_the_new_case(self):
        f = self.f
        self.rename()
        config = read_json(f.config)
        new = forward(self.new)
        self.assertEqual(set(config["projects"]),
                         {new[0].upper() + new[1:], new[0].lower() + new[1:], new + "/sub", forward(f.sibling)})
        self.assertEqual(config["githubRepoPaths"], {"u/proj": [self.new], "u/other": [f.sibling]})
        with open(f.history, encoding="utf-8") as fh:
            self.assertEqual([json.loads(line)["project"] for line in fh], [self.new, f.sibling])

    def test_editor_state_is_copied_to_the_new_id(self):
        f = self.f
        old_id = editor_state.workspace_id(f.src)
        self.rename()
        new_id = editor_state.workspace_id(self.new)
        self.assertNotEqual(new_id, old_id)  # the id depends on the exact case of the path
        storage = os.path.join(f.storage_root, new_id)
        self.assertEqual(read_json(os.path.join(storage, "workspace.json")), {"folder": editor_state.folder_uri(self.new)})
        self.assertTrue(editor_state.folder_uri(self.new).endswith("/proj"))
        tabs = json.loads(f.editor_item(storage, editor_state.CLAUDE_EXTENSION_KEY))
        self.assertEqual(tabs["panelTabSessions"][0]["title"], "Mi sesión")
        self.assertEqual(tabs["log"], self.new)
        self.assertEqual(json.loads(f.editor_item(f.old_storage, editor_state.CLAUDE_EXTENSION_KEY))["log"], f.src)

    def test_nothing_is_archived_and_verify_passes(self):
        f = self.f
        self.rename()
        self.assertFalse(os.path.exists(os.path.join(self.new, finalize.NOTE_FILE)))
        self.assertFalse([n for n in os.listdir(self.parent) if n.startswith("archive-")])
        record = f.record()
        self.assertEqual((record["kind"], record["phase1"], record["phase2"]), ("case-rename", "ok", "n/a"))
        self.assertEqual(f.run("verify", self.new), 0, f.output)
        self.assertEqual(f.run("verify", "--all"), 0, f.output)
        self.assertEqual(f.run("archive", self.new), 2)
        self.assertEqual(f.run("status"), 0, f.output)

    def test_siblings_are_not_touched(self):
        f = self.f
        before = {n: read(os.path.join(f.projects, n, "s.jsonl"))
                  for n in (paths.project_dir_name(f.sibling), paths.project_dir_name(f.prefix_sibling))}
        self.rename()
        self.assertTrue({"Proj2", "Proj-old"} <= set(os.listdir(self.parent)))
        for name, text in before.items():
            self.assertEqual(read(os.path.join(f.projects, name, "s.jsonl")), text)

    def test_renaming_back_is_not_reverted_by_verify_all(self):
        f = self.f
        self.rename()
        self.assertEqual(f.run("migrate", self.new, f.src), 0, f.output)  # proj -> Proj again
        self.assertEqual(f.run("verify", "--all"), 0, f.output)
        self.assertIn("Later moved or renamed", f.output)
        self.assertIn("Proj", os.listdir(self.parent))
        self.assertIn(f.src_dir_name, os.listdir(f.projects))
        self.assertIn(in_json(f.src) + "\\\\", read(os.path.join(f.projects, f.src_dir_name, f.transcript_name)))

    def test_redo_after_undo_replaces_the_older_editor_state(self):
        f = self.f
        self.rename()
        self.assertEqual(f.run("undo", self.new), 0, f.output)
        # VS Code is used under the old name again: its state changes and becomes newer than the first copy.
        con = sqlite3.connect(os.path.join(f.old_storage, "state.vscdb"))
        tabs = json.dumps({"panelTabSessions": [{"sessionId": UUID, "title": "Nueva"}], "log": f.src})
        con.execute("UPDATE ItemTable SET value = ? WHERE key = ?", (tabs, editor_state.CLAUDE_EXTENSION_KEY))
        con.commit()
        con.close()
        later = os.path.getmtime(os.path.join(f.old_storage, "state.vscdb")) + 60
        os.utime(os.path.join(f.old_storage, "state.vscdb"), (later, later))
        self.rename()
        storage = os.path.join(f.storage_root, editor_state.workspace_id(self.new))
        tabs = json.loads(f.editor_item(storage, editor_state.CLAUDE_EXTENSION_KEY))
        self.assertEqual(tabs["panelTabSessions"][0]["title"], "Nueva")
        report_dir = f.record()["report_dir"]
        self.assertTrue(os.path.isdir(os.path.join(report_dir, "replaced-editor-state", "Code")))
        self.assertNotIn("WARNING", f.output)

    def test_verify_does_not_relink_a_folder_renamed_by_hand(self):
        f = self.f
        self.rename()
        os.rename(self.new, f.src)
        self.assertEqual(f.run("verify", self.new), 1, f.output)
        self.assertIn("on disk", f.output)
        self.assertIn(paths.project_dir_name(self.new), os.listdir(f.projects))  # left as it was

    def test_dry_run_says_in_place_and_changes_nothing(self):
        f = self.f
        self.assertEqual(f.run("migrate", "--dry-run", f.src, self.new), 0, f.output)
        self.assertIn("Case-only rename in place: Proj -> proj", f.output)
        self.assertIn("Mi sesión", f.output)
        self.assertIn("Proj", os.listdir(self.parent))
        self.assertIn(f.src_dir_name, os.listdir(f.projects))
        self.assertEqual(Registry(f.state_dir).all(), [])

    def test_undo_restores_the_old_case(self):
        f = self.f
        self.rename()
        self.assertEqual(f.run("undo", self.new), 0, f.output)
        self.assertIn("Proj", os.listdir(self.parent))
        self.assertIn(f.src_dir_name, os.listdir(f.projects))
        self.assertIn(in_json(f.src) + "\\\\", read(os.path.join(f.projects, f.src_dir_name, f.transcript_name)))
        self.assertIn(forward(f.src), read_json(f.config)["projects"])
        self.assertEqual(f.record()["phase1"], "undone")

    def test_same_name_and_parent_case_are_refused(self):
        f = self.f
        self.assertEqual(f.run("migrate", f.src, f.src), 2)
        self.assertIn("nothing to rename", f.output)
        self.assertEqual(f.run("migrate", f.src, os.path.join(self.parent.upper(), "Proj")), 2)
        self.assertIn("parent folder", f.output)
        self.assertIn("Proj", os.listdir(self.parent))

    def test_short_names_keep_the_requested_case(self):
        f = self.f  # expanding an 8.3 name asks Windows, which answers with the case on disk ("Proj")
        buffer = ctypes.create_unicode_buffer(32768)
        ctypes.windll.kernel32.GetShortPathNameW(self.parent, buffer, len(buffer))
        if "~" not in buffer.value:
            self.skipTest("8.3 short names are disabled on this volume")
        self.assertEqual(f.run("migrate", os.path.join(buffer.value, "Proj"), os.path.join(buffer.value, "proj")),
                         0, f.output)
        self.assertIn("proj", os.listdir(self.parent))
        self.assertEqual(f.record()["dst"], self.new)
        self.assertEqual(f.run("verify", self.new), 0, f.output)

    def test_open_editor_window_blocks_the_rename(self):
        f = self.f
        lock = {"pid": os.getpid(), "workspaceFolders": [f.src], "ideName": "Visual Studio Code"}
        write(os.path.join(f.claude, "ide", "1.lock"), json.dumps(lock))
        self.assertEqual(f.run("migrate", f.src, self.new), 2)
        self.assertIn("VS Code window", f.output)
        self.assertIn("Proj", os.listdir(self.parent))


class MoveThenCaseRenameTest(FixtureTestCase):
    """A move (OneDrive\\...\\Proj -> repos\\Proj) followed by a case-only rename (repos\\Proj -> repos\\proj)."""

    def setUp(self):
        super().setUp()
        f = self.f
        self.final = os.path.join(os.path.dirname(f.dst), "proj")
        self.assertEqual(f.run("migrate", f.src, f.dst), 0, f.output)
        self.assertEqual(f.run("migrate", f.dst, self.final), 0, f.output)

    def test_verify_all_repairs_the_original_path_to_the_final_name(self):
        f = self.f
        config = read_json(f.config)
        config["projects"][forward(f.src)] = {"allowedTools": ["Z"]}  # as if a stale session wrote it back
        write(f.config, json.dumps(config, indent=2))
        self.assertEqual(f.run("verify", "--all"), 0, f.output)
        keys = read_json(f.config)["projects"]
        self.assertIn(forward(self.final), keys)
        self.assertNotIn(forward(f.src), keys)
        self.assertNotIn(forward(f.dst), keys)  # not the intermediate case either

    def test_undo_twice_reaches_the_original(self):
        f = self.f
        self.assertEqual(f.run("undo", self.final), 0, f.output)  # proj -> Proj
        self.assertIn("Proj", os.listdir(os.path.dirname(f.dst)))
        self.assertEqual(f.run("undo", f.dst), 0, f.output)  # repos\Proj -> OneDrive\...\Proj
        self.assertTrue(os.path.isdir(f.src))
        self.assertIn(forward(f.src), read_json(f.config)["projects"])


class MigrationTest(FixtureTestCase):
    """Each test starts from a complete migration and checks one part of the result."""

    def setUp(self):
        super().setUp()
        self.assertEqual(self.f.run("migrate", self.f.src, self.f.dst), 0, self.f.output)
        self.archive = os.path.join(os.path.dirname(self.f.src), f"archive-{dt.date.today().isoformat()}-Proj")

    def test_small_repo_prints_no_progress_lines(self):
        self.assertNotIn("% |", self.f.output)  # finished well before the first 60 s interval
        progress_file = os.path.join(self.f.record()["report_dir"], "progress.txt")
        self.assertIn("phase 1 finished", read(progress_file))

    def test_copy_has_content_without_dependencies(self):
        f = self.f
        self.assertEqual(read(os.path.join(f.dst, "a.txt")), "hola\n")
        self.assertTrue(os.path.exists(paths.long_path(os.path.join(f.dst, f.deep_rel))))
        self.assertTrue(os.path.isdir(os.path.join(f.dst, "empty")))
        self.assertTrue(os.path.isfile(os.path.join(f.dst, "packages", "p", "package.json")))
        for excluded in ("node_modules", os.path.join("packages", "p", "node_modules"), ".venv"):
            self.assertFalse(os.path.exists(os.path.join(f.dst, excluded)), excluded)

    def test_links_are_recreated_and_internal_ones_remapped(self):
        f = self.f

        def target(name):
            return paths.strip_long_prefix(os.readlink(os.path.join(f.dst, name)))

        self.assertTrue(paths.same_path(target("link_internal"), os.path.join(f.dst, "sub")))
        self.assertTrue(paths.same_path(target("link_external"), f.external))

    def test_original_is_archived(self):
        self.assertFalse(os.path.exists(self.f.src))
        self.assertTrue(os.path.isdir(self.archive))

    def test_sessions_are_moved_and_rewritten(self):
        f = self.f
        new_dir = os.path.join(f.projects, f.dst_dir_name)
        self.assertFalse(os.path.exists(os.path.join(f.projects, f.src_dir_name)))
        transcript = os.path.join(new_dir, f.transcript_name)
        text = read(transcript)
        for gone in ("OneDrive\\\\Desktop\\\\Proj\\\\", "OneDrive/Desktop/Proj/", f.src_dir_name + "/"):
            self.assertNotIn(gone, text)
        for present in (in_json(f.dst), git_bash(f.dst), in_json(f.sibling), "Proj.bak", f.dst_dir_name + "/memory"):
            self.assertIn(present, text)
        self.assertEqual(int(os.stat(transcript).st_mtime), OLD_MTIME)
        self.assertIn(f.dst, read(os.path.join(new_dir, UUID, "tool-results", "t.txt")))
        self.assertIn(f.dst, read(os.path.join(new_dir, "memory", "MEMORY.md")))

    def test_subfolder_sessions_move_and_siblings_stay(self):
        f = self.f
        self.assertTrue(os.path.isdir(os.path.join(f.projects, f.dst_dir_name + "-sub")))
        self.assertTrue(os.path.isdir(os.path.join(f.projects, paths.project_dir_name(f.sibling))))
        self.assertTrue(os.path.isdir(os.path.join(f.projects, paths.project_dir_name(f.prefix_sibling))))

    def test_config_and_history_point_to_new_path(self):
        f = self.f
        config = read_json(f.config)
        keys = set(config["projects"])
        new = forward(f.dst)
        self.assertTrue({new[0].upper() + new[1:], new[0].lower() + new[1:], new + "/sub", forward(f.sibling)} <= keys)
        self.assertNotIn(forward(f.src).lower(), {k.lower() for k in keys})
        self.assertEqual(config["githubRepoPaths"], {"u/proj": [f.dst], "u/other": [f.sibling]})
        with open(f.history, encoding="utf-8") as fh:
            self.assertEqual([json.loads(line)["project"] for line in fh], [f.dst, f.sibling])

    def test_note_report_and_registry(self):
        f = self.f
        note = read(os.path.join(f.dst, "CLAUDE.local.md"))
        self.assertIn(finalize.NOTE_START, note)
        self.assertIn(self.archive, note)
        self.assertIn("`node_modules`, `packages/p/node_modules`", note)
        self.assertIn("CLAUDE.local.md", read(os.path.join(f.dst, ".git", "info", "exclude")))
        status = subprocess.run(["git", "-C", f.dst, "status", "--porcelain"], capture_output=True, text=True)
        self.assertNotIn("CLAUDE.local.md", status.stdout)
        record = f.record()
        self.assertEqual(record["phase1"], "ok")
        report = read_json(os.path.join(record["report_dir"], "report.json"))
        self.assertEqual([x["path"] for x in report["stale_refs"]], ["config.txt"])
        self.assertEqual(report["venvs"][0]["python_version"], "3.13.7")
        self.assertIn("pnpm-lock.yaml", report["lockfiles"])

    def test_editor_state_is_copied_for_the_new_folder(self):
        f = self.f
        new_storage = os.path.join(f.storage_root, editor_state.workspace_id(f.dst))
        self.assertEqual(read_json(os.path.join(new_storage, "workspace.json")),
                         {"folder": editor_state.folder_uri(f.dst)})
        tabs = json.loads(f.editor_item(new_storage, editor_state.CLAUDE_EXTENSION_KEY))
        self.assertEqual(tabs["panelTabSessions"][0]["title"], "Mi sesión")
        self.assertEqual(tabs["log"], f.dst)
        memento = f.editor_item(new_storage, "memento/workbench.editors.files.textFileEditor")
        self.assertIn(editor_state.folder_uri(f.dst) + "/a.txt", memento)
        self.assertIn(quote(in_json(f.dst), safe=""), memento)  # JSON inside a URI query
        self.assertNotIn(quote(in_json(f.src), safe=""), memento)
        self.assertIn(in_json(f.dst.upper()), f.editor_item(new_storage, "ms-python.python"))
        self.assertIn(in_json(f.dst), read(os.path.join(new_storage, "chatSessions", "c.jsonl")))
        self.assertIn(in_json(f.src), f.editor_item(f.old_storage, editor_state.CLAUDE_EXTENSION_KEY))  # untouched
        report_dir = f.record()["report_dir"]
        self.assertEqual(read_json(os.path.join(report_dir, "report.json"))["editor_state"][0]["status"], "copied")
        self.assertIn("Mi sesión", read(os.path.join(report_dir, "report.md")))

    def test_verify_repairs_a_reverted_config(self):
        f = self.f
        self.assertEqual(f.run("verify", f.dst), 0, f.output)
        config = read_json(f.config)
        config["projects"][forward(f.src)] = {"allowedTools": ["Z"]}  # as if an open session rewrote it
        write(f.config, json.dumps(config, indent=2))
        self.assertEqual(f.run("verify", "--all"), 0, f.output)
        self.assertNotIn(forward(f.src), read_json(f.config)["projects"])

    def test_verify_does_not_repair_while_a_session_is_open(self):
        f = self.f
        config = read_json(f.config)
        config["projects"][forward(f.src)] = {"allowedTools": ["Z"]}
        write(f.config, json.dumps(config, indent=2))
        write(os.path.join(f.claude, "sessions", "1.json"), json.dumps({"pid": os.getpid(), "cwd": f.dst}))
        self.assertEqual(f.run("verify", f.dst), 1, f.output)
        self.assertIn("not repaired", f.output)
        self.assertIn(forward(f.src), read_json(f.config)["projects"])  # left for when the session is closed

    def test_verify_leaves_later_mentions_in_transcripts(self):
        f = self.f  # someone talked about the old path after the migration: that is conversation text
        transcript = os.path.join(f.projects, f.dst_dir_name, f.transcript_name)
        with open(transcript, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "cwd": f.dst, "text": f"it used to be in {f.src}"}) + "\n")
        self.assertEqual(f.run("verify", f.dst), 0, f.output)
        self.assertIn("left as it is", f.output)
        self.assertIn(in_json(f.src), read(transcript))

    def test_undo_restores_original_and_sessions(self):
        f = self.f
        self.assertEqual(f.run("undo", f.dst), 0, f.output)
        self.assertTrue(os.path.isdir(f.src))
        self.assertFalse(os.path.exists(self.archive))
        transcript = os.path.join(f.projects, f.src_dir_name, f.transcript_name)
        self.assertIn(in_json(f.src), read(transcript))
        self.assertIn(forward(f.src), read_json(f.config)["projects"])
        self.assertEqual(f.record()["phase1"], "undone")


class FakeClock:
    """Clock the tests move by hand."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class ProgressTest(unittest.TestCase):
    def test_prints_at_most_one_line_per_interval(self):
        clock = FakeClock()
        buf = io.StringIO()
        with redirect_stdout(buf):
            p = progress.Progress(300_000_000, interval=60, clock=clock)
            p.start_stage("copying", 100_000_000)
            for _ in range(10):  # 10 updates within the first minute: no line
                clock.now += 5
                p.advance(1_000_000)
            clock.now = 120
            p.advance(50_000_000)  # one line
            p.advance(1_000_000)  # same instant: no new line
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertEqual(lines[0], "  20% | copying 60 MB of 100 MB | 2 min elapsed, ~8 min left")

    def test_heartbeat_reports_scans_once_per_interval(self):
        clock = FakeClock()
        buf = io.StringIO()
        with redirect_stdout(buf):
            beat = progress.Heartbeat("scanning the source", interval=60, clock=clock)
            for files in range(1, 1001):
                clock.now = files * 0.15  # 150 s in total
                beat(files)
        self.assertEqual(buf.getvalue().splitlines(),
                         ["  scanning the source: 400 files so far", "  scanning the source: 800 files so far"])

    def test_copied_bytes_reads_robocopy_file_lines_in_any_language(self):
        self.assertEqual(copying.copied_bytes("\t    Nuevo arch\t\t  123456\tC:\\a\\b.bin\r\n"), 123456)
        self.assertEqual(copying.copied_bytes("\t    New File  \t\t       4\tC:\\a\\c.txt"), 4)
        for other in ("", " Log File : C:\\x.txt", "    Bytes:    123460    123460         0", "-" * 78):
            self.assertEqual(copying.copied_bytes(other), 0, other)


class EnvironmentTest(unittest.TestCase):
    def test_refuses_non_windows_and_old_python(self):
        import cc_move
        self.assertIsNone(cc_move.environment_problem("win32", (3, 13)))
        self.assertIn("Windows", cc_move.environment_problem("linux", (3, 13)))
        self.assertIn("3.8", cc_move.environment_problem("win32", (3, 7)))


class ConfirmHookTest(unittest.TestCase):
    """The plugin hook makes Claude Code ask before any cc-move command that changes something."""

    def setUp(self):
        sys.path.insert(0, os.path.join(REPO, "plugin", "hooks"))
        import confirm_changes
        self.needs_approval = confirm_changes.needs_approval

    def test_changing_commands_need_approval(self):
        for command in (r'python C:/p/cc_move.py migrate "C:\a" "C:\b"',
                        r'python C:/p/cc_move.py migrate "C:\a" "C:\b" --with-deps',
                        r'python C:/p/cc_move.py --state-dir "C:\s" undo "C:\b"',
                        r'cd C:\x && py -3 C:\p\cc_move.py archive "C:\b"',
                        r'python C:/p/cc_move.py relink "C:\a" "C:\b"',
                        r'python C:/p/cc_move.py prune',
                        r'python C:/p/cc_move.py prune --older-than 0'):
            self.assertTrue(self.needs_approval(command), command)

    def test_read_only_commands_do_not(self):
        for command in (r'python C:/p/cc_move.py migrate --dry-run "C:\a\archive" "C:\b"',
                        r'python C:/p/cc_move.py migrate "C:\a" "C:\b" --dry-run',
                        r'python C:/p/cc_move.py verify "C:\x\archive-2026-10-14-demo"',
                        r'python C:/p/cc_move.py status',
                        r'python C:/p/cc_move.py prune --dry-run',
                        r'git mv migrate undo'):
            self.assertFalse(self.needs_approval(command), command)


class GuardHookTest(unittest.TestCase):
    """The skill's hook blocks commits, pushes and deploys, and nothing else (not even commands with $VARIABLES)."""

    SCRIPT = os.path.join(REPO, "plugin", "hooks", "guard_migration.py")

    def setUp(self):
        sys.path.insert(0, os.path.dirname(self.SCRIPT))
        import guard_migration
        self.blocked_reason = guard_migration.blocked_reason

    def test_blocks_commits_pushes_and_deploys(self):
        for command in ('git commit -m "x"', "cd C:/x && git push origin main", r'git -C "C:\a b" push',
                        "git -c user.name=x commit -m y", "GIT_DIR=x git commit", "& git push",
                        "echo ok; terraform apply", "npm publish", "vercel --prod", "firebase deploy",
                        "sls deploy", "echo $(git push)"):
            self.assertIsNotNone(self.blocked_reason(command), command)

    def test_allows_everything_else(self):
        for command in ('du -sh "$LOCALAPPDATA/cc-move"', 'echo "$HOME"', "git status", "git log --oneline -5",
                        'grep -n "git push" README.md', "echo git commit", "python cc_move.py verify --all",
                        "git diff && git status"):
            self.assertIsNone(self.blocked_reason(command), command)

    def run_hook(self, command):
        """Runs the hook script as Claude Code does: the tool call as JSON on stdin."""
        return subprocess.run([sys.executable, self.SCRIPT], input=json.dumps({"tool_input": {"command": command}}),
                              capture_output=True, text=True)

    def test_exit_code_and_message(self):
        blocked = self.run_hook("git push")
        self.assertEqual(blocked.returncode, 2)
        self.assertIn("Blocked by cc-move", blocked.stderr)
        self.assertEqual(self.run_hook('du -sh "$LOCALAPPDATA"').returncode, 0)


class PathsTest(unittest.TestCase):
    def test_short_names_are_expanded(self):
        tmp = tempfile.mkdtemp(prefix="ccmove-short-")
        long_dir = os.path.join(tmp, "a long folder name")
        os.makedirs(long_dir)
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            ctypes.windll.kernel32.GetShortPathNameW(long_dir, buffer, len(buffer))
            if "~" not in buffer.value:
                self.skipTest("8.3 short names are disabled on this volume")
            self.assertEqual(paths.full(buffer.value), paths.full(long_dir))
            self.assertEqual(paths.full(os.path.join(buffer.value, "new")), os.path.join(paths.full(long_dir), "new"))
        finally:
            shutil.rmtree(tmp)


class EditorStateTest(unittest.TestCase):
    def test_workspace_id_matches_vscode_formula(self):
        folder = tempfile.mkdtemp(prefix="ccmove-id-")
        try:
            birth_ms = os.stat(folder).st_birthtime_ns // 1_000_000
            path = editor_state.vscode_path(folder)
            self.assertTrue(path[0].islower())
            expected = hashlib.md5((path + str(birth_ms)).encode("utf-8")).hexdigest()
            self.assertEqual(editor_state.workspace_id(folder), expected)
        finally:
            os.rmdir(folder)

    def test_folder_uri_encodes_like_vscode(self):
        folder = os.path.join(tempfile.mkdtemp(prefix="ccmove-uri-"), "A B")
        os.makedirs(folder)
        uri = editor_state.folder_uri(folder)
        self.assertTrue(uri.startswith("file:///c%3A/"), uri)
        self.assertTrue(uri.endswith("/A%20B"), uri)
        shutil.rmtree(os.path.dirname(folder))


class WithDepsTest(FixtureTestCase):
    """--with-deps copies node_modules and venvs, and includes them in the hash verification."""

    def test_dry_run_shows_dependency_sizes(self):
        f = self.f
        self.assertEqual(f.run("migrate", f.src, f.dst, "--dry-run"), 0, f.output)
        self.assertIn("--with-deps to copy them", f.output)
        self.assertIn("total:", f.output)

    def test_dependencies_are_copied_and_pnpm_links_remapped(self):
        f = self.f
        self.assertEqual(f.run("migrate", f.src, f.dst, "--with-deps"), 0, f.output)
        self.assertEqual(read(os.path.join(f.dst, "node_modules", "x", "index.js")), "x\n")
        self.assertTrue(os.path.isfile(os.path.join(f.dst, ".venv", "pyvenv.cfg")))
        link = paths.strip_long_prefix(os.readlink(os.path.join(f.dst, "node_modules", "p")))
        self.assertTrue(paths.same_path(link, os.path.join(f.dst, "packages", "p")))
        record = f.record()
        self.assertTrue(record["with_deps"])
        manifest = read(os.path.join(record["report_dir"], "manifest-src.tsv"))
        self.assertIn("F\tnode_modules/x/index.js", manifest)
        self.assertIn("copied as they were", read(os.path.join(f.dst, "CLAUDE.local.md")))


class BackupTest(FixtureTestCase):
    """Each migration leaves a zip of Claude Code's state that expires in 30 days; prune and migrate clean up."""

    def legacy_backup(self, name, date):
        """A folder backup and its registry record, as cc-move 0.2 left them."""
        folder = os.path.join(self.f.claude, "backups", "cc-move", name)
        write(os.path.join(folder, "claude.json"), "{}")
        write(os.path.join(folder, "projects", "c--x", "s.jsonl"), "{}\n" * 1000)
        record = {"id": name, "date": date.isoformat(), "src": r"C:\x", "dst": r"C:\y", "archive": None,
                  "phase1": "ok", "phase2": "ok", "backup_dir": folder}
        Registry(self.f.state_dir).save(record)
        return folder

    def expire(self, record_id):
        """Moves the expiry of a migration's backup to yesterday."""
        registry = Registry(self.f.state_dir)
        record = next(r for r in registry.all() if r["id"] == record_id)
        record["backup_expires"] = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        registry.save(record)

    def test_migration_leaves_a_zip_that_expires_in_30_days(self):
        f = self.f
        self.assertEqual(f.run("migrate", f.src, f.dst), 0, f.output)
        record = f.record()
        self.assertTrue(record["backup"].endswith(".zip") and os.path.isfile(record["backup"]), record)
        self.assertNotIn("backup_dir", record)
        self.assertEqual(record["backup_expires"], (dt.date.today() + dt.timedelta(days=30)).isoformat())
        with zipfile.ZipFile(record["backup"]) as z:
            names = set(z.namelist())
            self.assertIn(forward(f.src), json.loads(z.read("claude.json"))["projects"])  # the state before
        self.assertTrue({"claude.json", "history.jsonl", f"projects/{f.src_dir_name}/{f.transcript_name}",
                         f"projects/{f.src_dir_name}-sub/s.jsonl"} <= names, names)

    def test_prune_deletes_expired_backups_and_dry_run_does_not(self):
        f = self.f
        self.assertEqual(f.run("migrate", f.src, f.dst), 0, f.output)
        backup = f.record()["backup"]
        self.expire(f.record()["id"])
        self.assertEqual(f.run("prune", "--dry-run"), 0, f.output)
        self.assertIn("would delete", f.output)
        self.assertTrue(os.path.isfile(backup))
        self.assertEqual(f.run("prune"), 0, f.output)
        self.assertFalse(os.path.exists(backup))
        self.assertEqual((f.record()["backup"], f.record()["backup_pruned"]), (None, dt.date.today().isoformat()))
        self.assertEqual(f.run("status"), 0, f.output)
        self.assertIn("backup=deleted", f.output)

    def test_older_than_deletes_recent_backups_too(self):
        f = self.f
        self.assertEqual(f.run("migrate", f.src, f.dst), 0, f.output)
        backup = f.record()["backup"]
        self.assertEqual(f.run("prune"), 0, f.output)
        self.assertIn("Nothing to prune", f.output)
        self.assertTrue(os.path.isfile(backup))
        self.assertEqual(f.run("prune", "--older-than", "0"), 0, f.output)
        self.assertFalse(os.path.exists(backup))

    def test_pruning_keeps_the_registry_in_order(self):
        f = self.f  # verify follows later migrations in registry order
        self.legacy_backup("20260801-100000-old", dt.date.today() - dt.timedelta(days=40))
        self.assertEqual(f.run("migrate", f.src, f.dst), 0, f.output)
        self.assertEqual([r["id"] for r in Registry(f.state_dir).all()][0], "20260801-100000-old")

    def test_folder_backups_of_older_versions_are_zipped_or_deleted(self):
        f = self.f
        recent = self.legacy_backup("20261014-111935-recent", dt.date.today())
        old = self.legacy_backup("20260801-100000-old", dt.date.today() - dt.timedelta(days=40))
        stranger = os.path.join(f.claude, "backups", "cc-move", "copied-by-hand")
        write(os.path.join(stranger, "x.txt"), "x")
        self.assertEqual(f.run("prune"), 0, f.output)
        self.assertFalse(os.path.exists(recent) or os.path.exists(old))
        records = {r["id"]: r for r in Registry(f.state_dir).all()}
        zipped = records["20261014-111935-recent"]
        self.assertEqual(zipped["backup"], recent + ".zip")
        self.assertNotIn("backup_dir", zipped)
        self.assertEqual(zipped["backup_expires"], (dt.date.today() + dt.timedelta(days=30)).isoformat())
        with zipfile.ZipFile(zipped["backup"]) as z:
            self.assertEqual(sorted(z.namelist()), ["claude.json", "projects/c--x/s.jsonl"])
        self.assertIsNone(records["20260801-100000-old"]["backup"])
        self.assertTrue(os.path.isdir(stranger))  # unknown to cc-move: listed, never deleted
        self.assertIn("copied-by-hand", f.output)

    def test_migrate_prunes_expired_backups(self):
        f = self.f
        old = self.legacy_backup("20260801-100000-old", dt.date.today() - dt.timedelta(days=40))
        self.assertEqual(f.run("migrate", "--dry-run", f.src, f.dst), 0, f.output)
        self.assertIn("would delete", f.output)
        self.assertTrue(os.path.isdir(old))
        self.assertEqual(f.run("migrate", f.src, f.dst), 0, f.output)
        self.assertIn("Backups of earlier migrations", f.output)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.isfile(f.record()["backup"]))  # the new one stays


class NoteTest(unittest.TestCase):
    """The CLAUDE.local.md note mentions dependency folders only if the scan found some."""

    RECORD = {"src": r"C:\a\Proj", "date": "2026-10-14", "delete_after": "2027-04-14", "archive": r"C:\a\x"}

    def test_no_dependency_folders_means_no_line(self):
        note = finalize.note_text(dict(self.RECORD, dep_dirs=[]))
        self.assertNotIn("node_modules", note)
        self.assertNotIn("not copied", note)

    def test_dependency_folders_are_listed(self):
        note = finalize.note_text(dict(self.RECORD, dep_dirs=["node_modules", ".venv"]))
        self.assertIn("(`node_modules`, `.venv`) were not copied", note)
        note = finalize.note_text(dict(self.RECORD, dep_dirs=["node_modules"], with_deps=True))
        self.assertIn("(`node_modules`) were copied as they were", note)

    def test_records_of_older_versions_keep_the_generic_line(self):
        self.assertIn("`node_modules` and the Python virtual environments were not copied",
                      finalize.note_text(self.RECORD))


if __name__ == "__main__":
    unittest.main()
