"""Tests for the finder extension, with `rozi` replaced by a recorder and Git run for real.

Run from the repository root:

    python3 -m unittest discover tests -p 'test_*.py'
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

import finder  # noqa: E402

REAL_RUN = subprocess.run


def git(root: Path, *args: str) -> None:
    REAL_RUN(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        check=True,
        capture_output=True,
    )


def write(root: Path, relative: str, age: float = 0, text: str = "x") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    stamp = path.stat().st_mtime - age
    os.utime(path, (stamp, stamp))


class TempDirTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()


class ListingTest(TempDirTest):
    def test_a_repository_lists_what_git_does_newest_first(self) -> None:
        git(self.root, "init", "-q")
        write(self.root, ".gitignore", age=900, text="build/\n")
        write(self.root, "src/old.rs", age=600)
        write(self.root, "src/api/new.rs", age=10)
        write(self.root, "docs/gone.md", age=300)
        write(self.root, "build/out.bin")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "init")
        (self.root / "docs/gone.md").unlink()
        write(self.root, "notes.txt", age=60)  # untracked, not ignored

        files, dirs = finder.tree_entries(str(self.root), True, hidden=False)

        self.assertEqual(
            [entry.label for entry in files],
            ["src/api/new.rs", "notes.txt", "src/old.rs", ".gitignore"],
        )
        # A directory is as recent as the newest file anywhere under it.
        self.assertEqual([entry.label for entry in dirs], [".", "src/", "src/api/"])
        self.assertEqual(dirs[0].path, str(self.root))

    def test_outside_a_repository_dot_entries_are_listed_only_when_asked(self) -> None:
        write(self.root, "a.txt")
        write(self.root, ".cache/blob")
        write(self.root, ".git/config")
        (self.root / "empty").mkdir()

        files, dirs = finder.tree_entries(str(self.root), False, hidden=False)
        self.assertEqual([entry.label for entry in files], ["a.txt"])
        self.assertEqual([entry.label for entry in dirs], [".", "empty/"])

        files, _ = finder.tree_entries(str(self.root), False, hidden=True)
        self.assertEqual(sorted(entry.label for entry in files), [".cache/blob", "a.txt"])

    def test_a_repository_inside_a_plain_directory_keeps_its_ignored_files_out(self) -> None:
        project = self.root / "project"
        project.mkdir()
        git(project, "init", "-q")
        write(project, ".gitignore", text="target/\n")
        write(project, "src/main.rs")
        write(project, "target/debug/app")
        write(self.root, "notes.txt")

        files, dirs = finder.tree_entries(str(self.root), False, hidden=False)

        self.assertEqual(
            sorted(entry.label for entry in files),
            ["notes.txt", "project/.gitignore", "project/src/main.rs"],
        )
        self.assertEqual(sorted(entry.label for entry in dirs), [".", "project/", "project/src/"])

    def test_changes_are_grouped_and_a_rename_lists_only_its_new_path(self) -> None:
        git(self.root, "init", "-q")
        for name in ("keep.rs", "moved.rs", "staged.rs"):
            write(self.root, name, text=name * 20)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "init")
        write(self.root, "keep.rs", text="changed")
        git(self.root, "mv", "moved.rs", "renamed.rs")
        write(self.root, "staged.rs", text="staged")
        git(self.root, "add", "staged.rs")
        write(self.root, "new dir/file.rs")

        entries = finder.changed_entries(str(self.root), str(self.root))

        self.assertEqual(
            [(entry.group, entry.label, entry.description) for entry in entries],
            [
                ("Changed", "keep.rs", "modified"),
                ("Staged", "renamed.rs", "renamed"),
                ("Staged", "staged.rs", "modified"),
                ("Untracked", "new dir/", "untracked"),
            ],
        )
        self.assertTrue(entries[-1].is_dir)


class JumpTest(unittest.TestCase):
    def test_other_panes_come_before_zoxide_without_repeats(self) -> None:
        panes = [
            {"id": 1, "cwd": "/work/here", "foreground_program": None},
            {"id": 2, "cwd": "/work/api", "foreground_program": "nvim"},
            {"id": 3, "cwd": "/work/api", "foreground_program": None},
            {"id": 4, "cwd": None},
        ]
        with mock.patch.object(finder, "run", return_value="/work/api\n/work/here\n/srv/site\n"):
            entries = finder.jump_entries(panes, "/work/here")
        self.assertEqual(
            [(entry.group, entry.path, entry.description) for entry in entries],
            [("Panes", "/work/api", "nvim"), ("Frequent", "/srv/site", "")],
        )


class SearchTest(unittest.TestCase):
    def test_the_closest_name_ranks_first_and_ties_keep_their_order(self) -> None:
        labels = [
            "src/sidebar/tree.rs",
            "tests/my_sidebar.rs",
            "docs/s_i_d_e_bar.md",
            "src/sidebar_tree.rs",
            "docs/sidebar.md",
            "src/sidebar/rows.rs",
            "src/store/mod.rs",
        ]
        entries = [finder.Entry("/r/" + label, label, False) for label in labels]
        self.assertEqual(
            [entry.label for entry in finder.search(entries, "SideBar")],
            [
                "docs/sidebar.md",  # the name itself
                "src/sidebar_tree.rs",  # a name starting with it
                "tests/my_sidebar.rs",  # a name containing it
                "src/sidebar/tree.rs",  # paths containing it, in the order given
                "src/sidebar/rows.rs",
                "docs/s_i_d_e_bar.md",  # its letters in order
            ],
        )
        self.assertEqual(
            [entry.label for entry in finder.search(entries, "store mod")],
            ["src/store/mod.rs"],
        )
        self.assertEqual(finder.search(entries, "  "), entries)


class TypedPathTest(unittest.TestCase):
    def typed(self, path: str, is_dir: bool = False) -> str:
        return finder.typed_path(finder.Entry(path, "", is_dir), "/repo/src", "/repo")

    def test_paths_are_relative_inside_the_project_and_quoted_for_a_shell(self) -> None:
        self.assertEqual(self.typed("/repo/src/main.rs"), "main.rs")
        self.assertEqual(self.typed("/repo/docs/my notes.md"), "'../docs/my notes.md'")
        self.assertEqual(self.typed("/repo/src/-flag"), "./-flag")
        self.assertEqual(self.typed("/repo/src/api", is_dir=True), "api/")
        self.assertEqual(self.typed("/repo/src", is_dir=True), "./")
        # A sibling that only shares a name prefix is not inside the project.
        self.assertEqual(self.typed("/repo-old/x"), "/repo-old/x")


class FakeRozi:
    """Records every `rozi` call and answers `list-panes` with the given panes."""

    def __init__(self, panes: list[dict[str, object]], capabilities: list[str] = []) -> None:
        self.panes = panes
        self.capabilities = capabilities
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_options):
        if argv[0] != finder.ROZI:
            return REAL_RUN(argv, **_options)
        self.calls.append(list(argv[1:]))
        stdout = ""
        if argv[1] == "list-panes":
            stdout = json.dumps({"ok": True, "data": self.panes})
        elif argv[1] == "api":
            stdout = json.dumps({"capabilities": self.capabilities})
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    def sent(self) -> list[list[str]]:
        return [call for call in self.calls if call[0] not in {"list-panes", "pick", "api"}]


class FakePicker:
    """A `rozi pick --json` process that replies with canned lines."""

    def __init__(self, replies: list[dict[str, object]]) -> None:
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("".join(json.dumps(reply) + "\n" for reply in replies))
        self.written = ""
        self.stdin.close = lambda: setattr(self, "written", self.stdin.getvalue())

    def wait(self) -> int:
        return 0

    def lines(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.written.splitlines()]


class FinderTest(TempDirTest):
    def finder(self, fake: FakeRozi, tab: str = "files", **config: object) -> finder.Finder:
        environment = {
            "ROZI_EXTENSION": "finder",
            "ROZI_SOURCE_PANE": "7",
            "ROZI_EXTENSION_CONFIG": json.dumps(config),
            "EDITOR": "vi",
        }
        patches = [
            mock.patch.dict(os.environ, environment),
            mock.patch.object(finder.subprocess, "run", fake),
            mock.patch.object(finder.shutil, "which", return_value=None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return finder.Finder(str(self.root), tab)

    def serve(self, instance: finder.Finder, *replies: dict[str, object]) -> FakePicker:
        picker = FakePicker(list(replies))
        with mock.patch.object(finder.subprocess, "Popen", return_value=picker):
            instance.serve()
        return picker

    def test_enter_types_the_quoted_path_into_the_source_pane(self) -> None:
        write(self.root, "my file.txt")
        fake = FakeRozi([{"id": 7, "cwd": str(self.root), "foreground_program": "claude"}])
        instance = self.finder(fake)
        picker = self.serve(
            instance, {"selected": str(self.root / "my file.txt"), "tab": "files"}
        )
        self.assertEqual(fake.sent(), [["send-text", "--target", "7", "'my file.txt' "]])
        request, *later = picker.lines()
        self.assertEqual([row["label"] for row in request["rows"]], ["my file.txt"])
        # Every other tab is filled without waiting for a switch.
        self.assertEqual({line["tab"] for line in later}, {"dirs", "changed", "jump"})

    def test_go_changes_directory_only_at_a_shell_prompt(self) -> None:
        write(self.root, "src/main.rs")
        target = str(self.root / "src")
        event = {"action": "go", "selected": str(self.root / "src/main.rs"), "tab": "files"}

        for program in (None, "zsh"):
            fake = FakeRozi([{"id": 7, "cwd": str(self.root), "foreground_program": program}])
            self.serve(self.finder(fake), event)
            self.assertEqual(
                fake.sent(),
                [
                    ["send-text", "--target", "7", f"cd -- {target}"],
                    ["send-keys", "--target", "7", "Enter"],
                ],
            )

        fake = FakeRozi([{"id": 7, "cwd": str(self.root), "foreground_program": "nvim"}])
        self.serve(self.finder(fake), event)
        self.assertEqual(fake.sent(), [["split", "--cwd", target, "--focus"]])

    def test_enter_on_the_jump_tab_goes_there(self) -> None:
        other = self.root / "other"
        other.mkdir()
        fake = FakeRozi(
            [
                {"id": 7, "cwd": str(self.root), "foreground_program": None},
                {"id": 8, "cwd": str(other), "foreground_program": None},
            ]
        )
        picker = self.serve(
            self.finder(fake, tab="jump"), {"selected": str(other), "tab": "jump"}
        )
        self.assertEqual(picker.lines()[0]["tab"], "jump")
        self.assertEqual(fake.sent()[0], ["send-text", "--target", "7", f"cd -- {other}"])

    def test_open_starts_the_editor_or_a_pane_in_a_directory(self) -> None:
        write(self.root, "src/main.rs")
        fake = FakeRozi([])
        self.serve(
            self.finder(fake, editor="code -w"),
            {"action": "open", "selected": str(self.root / "src/main.rs"), "tab": "files"},
        )
        self.assertEqual(
            fake.sent(),
            [
                [
                    "split",
                    "--cwd",
                    str(self.root),
                    "--focus",
                    "--argv",
                    "code",
                    "-w",
                    str(self.root / "src/main.rs"),
                ]
            ],
        )

        fake = FakeRozi([])
        self.serve(
            self.finder(fake),
            {"action": "open", "selected": str(self.root / "src"), "tab": "dirs"},
        )
        self.assertEqual(fake.sent(), [["split", "--cwd", str(self.root / "src"), "--focus"]])

    def test_search_all_reaches_entries_past_the_row_limit(self) -> None:
        for index in range(finder.MAX_ROWS + 5):
            write(self.root, f"pkg/mod_{index:04}.py", age=index)
        last = str(self.root / f"pkg/mod_{finder.MAX_ROWS + 4:04}.py")
        fake = FakeRozi([{"id": 7, "cwd": str(self.root), "foreground_program": None}])
        instance = self.finder(fake)

        picker = self.serve(
            instance,
            {"action": "search", "input": "0516", "tab": "files"},
            {"selected": last, "tab": "files"},
        )

        request = picker.lines()[0]
        self.assertEqual(len(request["rows"]), finder.MAX_ROWS)
        self.assertNotIn(last, [row["id"] for row in request["rows"]])
        self.assertIn("search", [action["id"] for action in request["actions"]])
        self.assertEqual(picker.lines()[-1], {"tab": "files", "rows": [mock.ANY]})
        self.assertEqual(picker.lines()[-1]["rows"][0]["id"], last)
        self.assertEqual(fake.sent(), [["send-text", "--target", "7", "pkg/mod_0516.py "]])

    def test_a_rozi_that_reports_search_text_is_answered_with_matching_rows(self) -> None:
        for index in range(finder.MAX_ROWS + 5):
            write(self.root, f"pkg/mod_{index:04}.py", age=index)
        last = str(self.root / f"pkg/mod_{finder.MAX_ROWS + 4:04}.py")
        fake = FakeRozi([], capabilities=["pick-query-events"])

        picker = self.serve(
            self.finder(fake),
            {"query": "05", "query_seq": 1, "tab": "files"},
            {"query": "0516", "query_seq": 2, "tab": "files"},
            # A grouped tab keeps its order, and the picker filters it.
            {"query": "pk", "tab": "changed"},
            {"selected": last, "tab": "files"},
        )

        request, *later = picker.lines()
        self.assertIs(request["report_query"], True)
        self.assertNotIn("search", [action["id"] for action in request["actions"]])
        answers = later[3:]  # after the three other tabs' opening rows
        self.assertEqual({line["tab"] for line in answers}, {"files"})
        self.assertEqual([row["id"] for row in answers[-1]["rows"]], [last])
        self.assertEqual(answers[-1]["query_seq"], 2)
        self.assertEqual(fake.sent(), [["send-text", "--target", "7", "pkg/mod_0516.py "]])

    def test_a_small_tree_offers_no_search_all(self) -> None:
        write(self.root, "a.txt")
        request = self.finder(FakeRozi([])).request()
        self.assertNotIn("search", [action["id"] for action in request["actions"]])
        self.assertNotIn("placeholder", request)

    def test_copy_without_a_clipboard_tool_says_so(self) -> None:
        write(self.root, "a.txt")
        fake = FakeRozi([])
        event = {"action": "copy", "selected": str(self.root / "a.txt"), "tab": "files"}
        with self.assertRaisesRegex(finder.FinderError, "clipboard"):
            self.serve(self.finder(fake), event)
        self.assertEqual(fake.sent(), [])


if __name__ == "__main__":
    unittest.main()
