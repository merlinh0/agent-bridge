"""Fixtures: a throwaway state directory, a git repository, and a fake Tern.

No test talks to a real terminal, omp or Claude Code.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_bridge import checkouts, server, store, tern
from agent_bridge.models import Models

CATALOG = [
    {
        "provider": "acme",
        "id": "small-1",
        "selector": "acme/small-1",
        "kind": "chat",
        "thinking": ["low", "high"],
        "cost": {"input": 0.1, "output": 0.5},
    },
    {
        "provider": "acme",
        "id": "mid-1",
        "selector": "acme/mid-1",
        "kind": "chat",
        "thinking": ["low", "medium", "high"],
        "cost": {"input": 1, "output": 5},
    },
    {
        "provider": "other",
        "id": "big-1",
        "selector": "other/big-1",
        "kind": "chat",
        "thinking": ["low", "medium", "high"],
        "cost": {"input": 5, "output": 25},
    },
]


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def state(tmp_path, monkeypatch):
    root = tmp_path / "state"
    for name, value in (
        ("STATE", root),
        ("TASKS", root / "tasks"),
        ("WORKTREES", root / "worktrees"),
        ("SESSIONS", root / "sessions"),
        ("CONFIG", root / "config.json"),
    ):
        monkeypatch.setattr(store, name, value)
    monkeypatch.setattr(server, "models", Models())
    monkeypatch.setattr(Models, "catalog", lambda self, refresh=False: CATALOG)
    monkeypatch.setattr(server, "RESTART_GRACE_SECONDS", 0)
    monkeypatch.setattr(checkouts, "BACKEND", "worktree")  # CI has no ovnfs; its own calls are checked live
    store.ensure_dirs()
    return root


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / "app.py").write_text("print('hi')\n")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "init")
    return path


class FakeTern:
    """Stands in for the `tern` module functions the server calls."""

    def __init__(self) -> None:
        self.blocks: dict[int, dict] = {}
        self.programs: dict[int, str] = {}
        self.spawned: list[list[str]] = []
        self.submitted: list[tuple[int, str]] = []
        self.pressed: list[tuple[int, list[str]]] = []
        self.closed: list[int] = []
        self._next = 100

    def add(self, program: str, **fields) -> int:
        self._next += 1
        block = self._next
        self.blocks[block] = {"id": block, "live": True, "exited": None, "session": "s", "window": "", "cwd": "/", **fields}
        self.programs[block] = program
        return block

    def scan(self) -> dict[int, dict]:
        return {k: dict(v) for k, v in self.blocks.items()}

    def spawn_tab(self, session, cwd, argv, title) -> int:
        self.spawned.append(argv)
        return self.add(Path(argv[0]).name, cwd=str(cwd))

    def foreground(self, block, window) -> list[str]:
        return [self.programs[block]] if block in self.programs else []

    def submit(self, block, window, text) -> None:
        self.submitted.append((block, text))

    def press(self, block, window, keys) -> None:
        self.pressed.append((block, keys))

    def close(self, block, window) -> bool:
        self.closed.append(block)
        return self.blocks.pop(block, None) is not None


@pytest.fixture
def fake_tern(monkeypatch):
    fake = FakeTern()
    for name in ("scan", "spawn_tab", "foreground", "submit", "press", "close"):
        monkeypatch.setattr(tern, name, getattr(fake, name))
    monkeypatch.setattr(server, "OMP", "/usr/local/bin/omp")
    monkeypatch.setattr(server, "CLAUDE", "/usr/local/bin/claude")
    return fake
