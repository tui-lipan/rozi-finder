#!/usr/bin/env python3
"""Find a file or directory in a rozi picker, then insert, visit, open, or copy its path."""

from __future__ import annotations

import json
import os
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass

ROZI = os.environ.get("ROZI_BIN", "rozi")

# rozi keeps at most this many rows of a snapshot; the rest are reached by searching.
MAX_ROWS = 512
# Entries read from a directory tree that is not a Git repository.
MAX_WALK = 50_000

TABS = [
    {"id": "files", "label": "Files"},
    {"id": "dirs", "label": "Directories"},
    {"id": "changed", "label": "Changed"},
    {"id": "jump", "label": "Jump"},
]

# Shells whose prompt takes `cd -- 'path'` with POSIX single quoting.
SHELLS = {"sh", "bash", "zsh", "fish", "dash", "ksh", "mksh", "ash"}

CLIPBOARD_TOOLS = [
    ("WAYLAND_DISPLAY", ["wl-copy"]),
    ("DISPLAY", ["xclip", "-selection", "clipboard"]),
    ("DISPLAY", ["xsel", "--clipboard", "--input"]),
    (None, ["pbcopy"]),
]


class FinderError(RuntimeError):
    """A failure worth one line in a notification."""


@dataclass(frozen=True)
class Entry:
    path: str  # absolute
    label: str
    is_dir: bool
    description: str = ""
    group: str = ""

    def row(self) -> dict[str, object]:
        row: dict[str, object] = {"id": self.path, "label": self.label}
        if self.description:
            row["description"] = self.description
        if self.group:
            row["group"] = self.group
        return row


def settings() -> dict[str, object]:
    try:
        value = json.loads(os.environ.get("ROZI_EXTENSION_CONFIG", "{}"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def run(argv: list[str], cwd: str | None = None) -> str | None:
    """A command's stdout, or None when it is missing or fails."""
    try:
        result = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, errors="replace", check=False
        )
    except OSError:
        return None
    return result.stdout if result.returncode == 0 else None


def rozi(*args: str) -> bool:
    try:
        result = subprocess.run([ROZI, *args], stdout=subprocess.DEVNULL, check=False)
        return result.returncode == 0
    except OSError:
        return False


def notify(message: str, error: bool = False) -> None:
    rozi("notify", message, "--title", "Finder", *(["--level", "error"] if error else []))


def reports_search_text() -> bool:
    """Whether this rozi's picker says what is typed, so a long list can follow the search."""
    try:
        description = json.loads(run([ROZI, "api", "describe"]) or "")
    except json.JSONDecodeError:
        return False
    capabilities = description.get("capabilities") if isinstance(description, dict) else None
    return isinstance(capabilities, list) and "pick-query-events" in capabilities


def repository_root(cwd: str) -> str | None:
    output = run(["git", "-C", cwd, "rev-parse", "--show-toplevel"])
    root = output.strip() if output else ""
    return root or None


def age(seconds: float) -> str:
    for limit, unit, size in (
        (60, "now", 0),
        (3600, "m", 60),
        (86_400, "h", 3600),
        (14 * 86_400, "d", 86_400),
        (60 * 86_400, "w", 7 * 86_400),
        (365 * 86_400, "mo", 30 * 86_400),
    ):
        if seconds < limit:
            return f"{int(seconds // size)}{unit}" if size else unit
    return f"{int(seconds // (365 * 86_400))}y"


def home_relative(path: str) -> str:
    home = os.path.expanduser("~")
    if path == home:
        return "~"
    return "~" + path[len(home) :] if path.startswith(home + os.sep) else path


def git_paths(base: str) -> list[str] | None:
    """Tracked and untracked-but-not-ignored paths under `base`, relative to it."""
    output = run(
        ["git", "-C", base, "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    )
    if output is None:
        return None
    return list(dict.fromkeys(path for path in output.split("\0") if path))


def walk_paths(base: str, hidden: bool) -> tuple[list[str], list[str]]:
    """Files and directories under `base`, nearest first, relative to it."""
    files: list[str] = []
    dirs: list[str] = []
    queue = deque([""])
    while queue and len(files) + len(dirs) < MAX_WALK:
        relative = queue.popleft()
        try:
            children = sorted(os.scandir(os.path.join(base, relative)), key=lambda e: e.name)
        except OSError:
            continue
        for child in children:
            if child.name == ".git" or (not hidden and child.name.startswith(".")):
                continue
            path = os.path.join(relative, child.name)
            try:
                if child.is_dir(follow_symlinks=False):
                    dirs.append(path)
                    queue.append(path)
                else:
                    files.append(path)
            except OSError:
                continue
    return files, dirs


def tree_entries(base: str, in_repository: bool, hidden: bool) -> tuple[list[Entry], list[Entry]]:
    """Files and directories under `base`, each most recently changed first."""
    listed = git_paths(base) if in_repository else None
    extra_dirs: list[str] = []
    if listed is None:
        listed, extra_dirs = walk_paths(base, hidden)

    now = time.time()
    files: list[tuple[float, Entry]] = []
    newest: dict[str, float] = {relative: 0.0 for relative in extra_dirs}
    for relative in listed:
        path = os.path.join(base, relative)
        try:
            status = os.stat(path)
        except OSError:
            continue  # deleted in the worktree, or a dangling link
        modified = status.st_mtime
        if os.path.isdir(path):  # a submodule or nested repository
            newest[relative] = max(newest.get(relative, 0.0), modified)
        else:
            files.append((modified, Entry(path, relative, False, age(now - modified))))
        parent = os.path.dirname(relative)
        while parent:
            if newest.get(parent, -1.0) >= modified:
                break  # every ancestor is already at least this recent
            newest[parent] = modified
            parent = os.path.dirname(parent)

    files.sort(key=lambda item: -item[0])
    dirs = [Entry(base, ".", True, home_relative(base))]
    for relative, modified in sorted(newest.items(), key=lambda item: (-item[1], item[0])):
        path = os.path.join(base, relative)
        if not modified:
            try:
                modified = os.stat(path).st_mtime
            except OSError:
                continue
        dirs.append(Entry(path, relative + "/", True, age(now - modified)))
    return [entry for _, entry in files], dirs


def change_kind(code: str) -> tuple[int, str, str]:
    """Sort rank, group, and description of a two-letter `git status` code."""
    if code == "??":
        return 3, "Untracked", "untracked"
    if "U" in code or code in {"AA", "DD"}:
        return 0, "Conflicts", "conflict"
    index, worktree = code[0], code[1]
    words = {"M": "modified", "D": "deleted", "A": "added", "R": "renamed", "T": "type changed"}
    if worktree != " ":
        return 1, "Changed", words.get(worktree, "changed")
    return 2, "Staged", words.get(index, "staged")


def changed_entries(root: str, base: str) -> list[Entry]:
    output = run(["git", "-C", root, "status", "--porcelain=v1", "-z"])
    if output is None:
        return []
    fields = output.split("\0")
    ranked: list[tuple[int, Entry]] = []
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if len(field) < 4:
            continue
        code, relative = field[:2], field[3:]
        if code[0] in "RC":
            index += 1  # the path it was renamed or copied from
        rank, group, description = change_kind(code)
        path = os.path.join(root, relative.rstrip("/"))
        is_dir = relative.endswith("/")
        label = os.path.relpath(path, base) + ("/" if is_dir else "")
        ranked.append((rank, Entry(path, label, is_dir, description, group)))
    ranked.sort(key=lambda item: item[0])
    return [entry for _, entry in ranked]


def session_panes() -> list[dict[str, object]]:
    output = run([ROZI, "list-panes", "--format", "json"])
    try:
        document = json.loads(output) if output else None
    except json.JSONDecodeError:
        return []
    data = document.get("data") if isinstance(document, dict) else document
    panes = data.get("panes") if isinstance(data, dict) else data
    return [pane for pane in panes if isinstance(pane, dict)] if isinstance(panes, list) else []


def jump_entries(panes: list[dict[str, object]], cwd: str) -> list[Entry]:
    """Directories away from here: where the session's other panes are, then zoxide's list."""
    entries: dict[str, Entry] = {}
    for pane in panes:
        path = pane.get("cwd")
        if not isinstance(path, str) or not os.path.isabs(path):
            continue
        path = os.path.normpath(path)
        if path == cwd or path in entries:
            continue
        program = pane.get("foreground_program")
        description = program if isinstance(program, str) else ""
        entries[path] = Entry(path, home_relative(path), True, description, "Panes")
    for path in (run(["zoxide", "query", "--list"]) or "").splitlines():
        if path and path != cwd and path not in entries:
            entries[path] = Entry(path, home_relative(path), True, "", "Frequent")
    return list(entries.values())


def is_subsequence(term: str, text: str) -> bool:
    characters = iter(text)
    return all(character in characters for character in term)


def search(entries: list[Entry], query: str) -> list[Entry]:
    """Entries matching every word of `query`: name matches first, then path, then fuzzy."""
    terms = query.lower().split()
    if not terms:
        return entries
    ranked: list[tuple[int, int, int, Entry]] = []
    for position, entry in enumerate(entries):
        label = entry.label.lower()
        name = os.path.basename(label.rstrip("/"))
        if all(term in name for term in terms):
            rank = 0
        elif all(term in label for term in terms):
            rank = 1
        elif all(is_subsequence(term, label) for term in terms):
            rank = 2
        else:
            continue
        ranked.append((rank, len(label) if rank else 0, position, entry))
    ranked.sort(key=lambda item: item[:3])
    return [item[3] for item in ranked]


def typed_path(entry: Entry, cwd: str, base: str) -> str:
    """The path as typed into the pane: relative when it is under the pane's project."""
    path = entry.path
    inside = any(path == top or path.startswith(top.rstrip(os.sep) + os.sep) for top in (cwd, base))
    if inside:
        path = os.path.relpath(path, cwd)
        if path.startswith("-"):
            path = "./" + path
    if entry.is_dir and not path.endswith("/"):
        path += "/"
    return shlex.quote(path)


def copy_to_clipboard(text: str) -> None:
    for variable, argv in CLIPBOARD_TOOLS:
        if variable and not os.environ.get(variable):
            continue
        if not shutil.which(argv[0]):
            continue
        try:
            subprocess.run(
                argv,
                input=text,
                text=True,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except (OSError, subprocess.CalledProcessError):
            continue
    raise FinderError("No clipboard tool found: install wl-clipboard, xclip, or xsel")


class Finder:
    def __init__(self, cwd: str, tab: str) -> None:
        config = settings()
        self.cwd = cwd
        self.pane = os.environ.get("ROZI_SOURCE_PANE", "")
        self.editor = str(config.get("editor") or "")
        root = repository_root(cwd)
        self.base = root if root and config.get("root") != "directory" else cwd
        self.panes = session_panes()
        files, dirs = tree_entries(self.base, root is not None, config.get("hidden") is True)
        self.entries: dict[str, list[Entry]] = {
            "files": files,
            "dirs": dirs,
            "changed": changed_entries(root, self.base) if root else [],
            "jump": jump_entries(self.panes, cwd),
        }
        self.tab = tab if tab in self.entries else "files"
        self.by_path = {
            tab: {entry.path: entry for entry in entries} for tab, entries in self.entries.items()
        }
        self.live = reports_search_text()

    def oversized(self, tab: str) -> bool:
        return len(self.entries[tab]) > MAX_ROWS

    def rows(self, tab: str, query: str = "") -> list[dict[str, object]]:
        return [entry.row() for entry in search(self.entries[tab], query)[:MAX_ROWS]]

    def request(self) -> dict[str, object]:
        request: dict[str, object] = {
            "title": f"Find in {os.path.basename(self.base) or self.base}",
            "width": 90,
            "empty": "Nothing here",
            "tabs": TABS,
            "tab": self.tab,
            "rows": self.rows(self.tab),
            "actions": [
                {"id": "go", "key": "ctrl-g", "label": "go", "close": True},
                {"id": "open", "key": "ctrl-o", "label": "open", "close": True},
                {"id": "copy", "key": "ctrl-y", "label": "copy", "close": True},
            ],
        }
        if self.live:
            request["report_query"] = True
        elif any(self.oversized(tab) for tab in self.entries):
            # An older rozi does not say what is typed: offer the whole tree behind a key.
            request["actions"].append(
                {
                    "id": "search",
                    "key": "ctrl-f",
                    "label": "search all",
                    "prompt": {"title": "Search every entry", "from_query": True},
                }
            )
            total = len(self.entries["files"])
            if total > MAX_ROWS:
                request["placeholder"] = f"Search recent files, or Ctrl+F for all {total:,}…"
        return request

    # Actions

    def insert(self, entry: Entry) -> None:
        text = typed_path(entry, self.cwd, self.base) + " "
        target = ["--target", self.pane] if self.pane else []
        if not rozi("send-text", *target, text):
            raise FinderError("Could not type into the pane")

    def at_shell_prompt(self) -> bool:
        for pane in self.panes:
            if str(pane.get("id")) == self.pane:
                # rozi names no foreground program while the pane's own shell waits at its prompt.
                program = pane.get("foreground_program")
                return program is None or program in SHELLS
        return False

    def go(self, entry: Entry) -> None:
        directory = entry.path if entry.is_dir else os.path.dirname(entry.path)
        if not os.path.isdir(directory):
            raise FinderError(f"{home_relative(directory)} is not a directory")
        if self.pane and self.at_shell_prompt():
            command = "cd -- " + shlex.quote(directory)
            if rozi("send-text", "--target", self.pane, command) and rozi(
                "send-keys", "--target", self.pane, "Enter"
            ):
                return
            raise FinderError("Could not type into the pane")
        # Something other than a shell has the pane: never type a command into it.
        if not rozi("split", "--cwd", directory, "--focus"):
            raise FinderError("Could not open a pane")

    def open(self, entry: Entry) -> None:
        if entry.is_dir:
            if not rozi("split", "--cwd", entry.path, "--focus"):
                raise FinderError("Could not open a pane")
            return
        editor = self.editor or os.environ.get("VISUAL") or os.environ.get("EDITOR") or ""
        argv = shlex.split(editor)
        if not argv:
            raise FinderError("No editor: set `editor` in [extensions.finder], or $EDITOR")
        if not rozi("split", "--cwd", self.base, "--focus", "--argv", *argv, entry.path):
            raise FinderError("Could not open a pane")

    def copy(self, entry: Entry) -> None:
        copy_to_clipboard(entry.path)
        notify(f"Copied {home_relative(entry.path)}")

    def act(self, action: str | None, tab: str, selected: object) -> None:
        entry = self.by_path[tab].get(selected) if isinstance(selected, str) else None
        if entry is None:
            return
        if action is None:
            action = "go" if tab == "jump" else "insert"
        handler = {"insert": self.insert, "go": self.go, "open": self.open, "copy": self.copy}
        if action in handler:
            handler[action](entry)

    # Picker stream

    def serve(self) -> None:
        try:
            process = subprocess.Popen(
                [ROZI, "pick", "--json"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as error:
            raise FinderError("rozi picker unavailable") from error
        assert process.stdin is not None and process.stdout is not None

        def send(payload: dict[str, object]) -> bool:
            try:
                process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                process.stdin.flush()
                return True
            except (BrokenPipeError, OSError, ValueError):
                return False

        # Replies are read on a thread so that search text typed faster than it can be answered
        # is answered once, for the newest text.
        replies: queue.Queue[dict[str, object] | None] = queue.Queue()

        def read_replies() -> None:
            for line in process.stdout:
                try:
                    reply = json.loads(line)
                except json.JSONDecodeError:
                    continue
                replies.put(reply if isinstance(reply, dict) else None)
            replies.put(None)

        threading.Thread(target=read_replies, daemon=True).start()
        waiting: list[dict[str, object] | None] = []

        def next_reply() -> dict[str, object] | None:
            reply = waiting.pop() if waiting else replies.get()
            while reply is not None and "query" in reply:
                try:
                    later = replies.get_nowait()
                except queue.Empty:
                    break
                if later is not None and "query" in later and later.get("tab") == reply.get("tab"):
                    reply = later
                else:
                    waiting.append(later)
                    break
            return reply

        try:
            alive = send(self.request())
            for tab in self.entries:
                if alive and tab != self.tab:
                    alive = send({"tab": tab, "rows": self.rows(tab)})
            while alive:
                event = next_reply()
                if event is None or event.get("cancelled"):
                    break
                tab = event.get("tab")
                if tab not in self.entries:
                    continue
                action = event.get("action")
                query = event.get("query", event.get("input") if action == "search" else None)
                if isinstance(query, str):
                    # A tab that fits in the picker is already filtered there.
                    if self.oversized(tab):
                        alive = send({"tab": tab, "rows": self.rows(tab, query)})
                elif action is not None or "selected" in event:
                    self.act(action, tab, event.get("selected"))
                    break
        finally:
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
            process.wait()


def main(arguments: list[str]) -> int:
    if not os.environ.get("ROZI_EXTENSION"):
        print("run this through rozi: rozi run-action finder.files", file=sys.stderr)
        return 2
    try:
        Finder(os.getcwd(), arguments[0] if arguments else "files").serve()
    except FinderError as error:
        # The notification is the report; a non-zero exit would add rozi's vaguer one.
        notify(str(error), error=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
