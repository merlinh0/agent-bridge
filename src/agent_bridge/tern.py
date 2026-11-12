"""Thin wrapper over the `tern` command line (https://docs.stencil.so/tern).

Everything the bridge does to a terminal goes through here. Text is always passed as one argv
element, after `--` where Tern accepts it, so nothing a caller supplies is parsed as an option
or by a shell.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import proc

EXE = proc.find_executable("tern", os.environ.get("AGENT_BRIDGE_TERN"), "/Applications/Tern.app/Contents/MacOS/tern")


def tern(*args: str, window: str | None = None, check: bool = True) -> str:
    if not EXE:
        raise RuntimeError("tern CLI not found; install Tern or set AGENT_BRIDGE_TERN")
    argv = list(args)
    if window is not None:
        argv[1:1] = ["--window", window]  # goes right after the subcommand
    return proc.run([EXE, *argv], check=check)


def _inspect(window: str | None) -> dict:
    return json.loads(tern("inspect", "--json", window=window))


def scan() -> dict[int, dict]:
    """Every block of every Tern window, by id.

    Each block is Tern's own record plus `session`, `window`, and for panes running omp what omp
    reports to Tern: `omp_session_file` (None once omp has exited) and `working`.

    Raises if a window cannot be read: a partial view would make a running agent look closed.
    """
    first = _inspect(None)
    windows = sorted({c.get("window", "") for c in first.get("clients", []) if c.get("kind") == "window"})
    if len(windows) > 1:
        # Block ids are only valid in their own window, so each window is inspected by key.
        views = {window: _inspect(window) for window in windows}
    else:
        views = {windows[0] if windows else None: first}
    blocks: dict[int, dict] = {}
    for window, view in views.items():
        panes = {p["pane"]: p for p in view.get("panes", [])}
        for session in view.get("sessions", []):
            for tab in session.get("tabs", []):
                for block in tab.get("blocks", []):
                    pane = panes.get(block["id"], {})
                    blocks[block["id"]] = {
                        **block,
                        "session": session["name"],
                        "window": window,
                        "omp_session_file": pane.get("omp_session_file"),
                        "working": pane.get("working"),
                    }
    return blocks


def spawn_tab(session: str, cwd: Path | str, argv: list[str], title: str) -> int:
    """Run `argv` in a new tab of `session` (created if needed) and return the block id."""
    existing = json.loads(tern("ls", "--json")).get("sessions", [])
    launch = ["--json", "--keep-open", "--cwd", str(cwd), "--", *argv]
    if any(s["name"] == session for s in existing):
        out = tern("new", "tab", session, *launch)
    else:
        out = tern("new", "session", session, *launch)
    block = int(json.loads(out)["block"])
    tern("rename", str(block), title, check=False)
    return block


def foreground(block: int, window: str | None) -> list[str]:
    """Command line of the program in the foreground of a block, its name first (`omp`, `claude`, `zsh`…).

    Empty if it cannot be read.
    """
    try:
        info = json.loads(tern("process", str(block), "--json", window=window))
    except (RuntimeError, json.JSONDecodeError):
        return []
    argv = [str(arg) for arg in (info.get("foreground") or {}).get("argv") or []]
    return [Path(argv[0]).name, *argv[1:]] if argv else []


def submit(block: int, window: str | None, text: str) -> None:
    """Paste `text` into a block and press Enter."""
    tern("send", str(block), "paste", "--", text, window=window)
    time.sleep(0.4)  # let the program take the paste before the Enter
    tern("send", str(block), "keys", "Enter", window=window)


def press(block: int, window: str | None, keys: list[str]) -> None:
    # `tern send … keys` has no `--` separator, so refuse anything it could read as an option.
    bad = [k for k in keys if not k or k.startswith("-") or any(c.isspace() for c in k)]
    if bad or not keys:
        raise ValueError(f"invalid keys {bad or keys}: use names such as Enter, Escape, Down, ctrl+c")
    tern("send", str(block), "keys", *keys, window=window)


def capture(block: int, window: str | None) -> str:
    return tern("capture", str(block), "--surfaces", window=window)


def close(block: int, window: str | None) -> bool:
    """Close a block. False when Tern refused or the block is already gone."""
    try:
        tern("close", str(block), window=window)
    except RuntimeError:
        return False
    return True
