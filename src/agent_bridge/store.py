"""On-disk state under ~/.agent-bridge: one JSON record per agent, worktrees, sessions, config."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import stat
import tempfile
from pathlib import Path

log = logging.getLogger("agent_bridge")

STATE = Path(os.environ.get("AGENT_BRIDGE_HOME") or Path.home() / ".agent-bridge")
TASKS = STATE / "tasks"
WORKTREES = STATE / "worktrees"
SESSIONS = STATE / "sessions"
CONFIG = STATE / "config.json"

# Agent names become file names, directory names and git branch names.
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_MAX_NAME = 48


def ensure_dirs() -> None:
    """Create the state tree, private to the user: it holds prompts, transcripts and source code."""
    STATE.mkdir(parents=True, exist_ok=True)
    STATE.chmod(0o700)
    for d in (TASKS, WORKTREES, SESSIONS):
        d.mkdir(exist_ok=True)


def write_json(path: Path, data: object) -> None:
    """Atomically replace `path`. Keeps the file's current permissions, or 0600 for a new file."""
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def slug(text: str) -> str:
    """A valid agent name made from the first words of `text`."""
    words = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-").split("-")
    return "-".join(words[:5])[:_MAX_NAME].strip("-") or "agent"


def task_file(name: str) -> Path:
    if len(name) > _MAX_NAME or not _NAME.fullmatch(name):
        raise ValueError(f"invalid agent name {name!r}: use lowercase letters, digits and dashes")
    return TASKS / f"{name}.json"


def unique_name(base: str) -> str:
    name, n = base, 2
    while task_file(name).exists():
        name, n = f"{base[: _MAX_NAME - 4]}-{n}", n + 1
    return name


def load(name: str) -> dict:
    path = task_file(name)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        known = ", ".join(t["name"] for t in all_tasks()) or "none"
        raise ValueError(f"unknown agent {name!r} (known: {known})") from None
    except json.JSONDecodeError as e:
        raise ValueError(f"state file {path} is corrupt: {e}") from e


def save(task: dict) -> None:
    ensure_dirs()
    write_json(task_file(task["name"]), task)


def forget(name: str) -> None:
    task_file(name).unlink(missing_ok=True)


def all_tasks() -> list[dict]:
    tasks = []
    for path in TASKS.glob("*.json"):
        try:
            tasks.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("skipping unreadable state file %s: %s", path, e)
    return sorted(tasks, key=lambda t: t.get("created_at", 0))


def read_config() -> dict:
    """The user's settings. A broken file is an error: ignoring it would silently lift the allowlist."""
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{CONFIG} is not valid JSON ({e}); fix or delete it") from e
    if isinstance(data, dict):
        return data
    raise RuntimeError(f"{CONFIG} must contain a JSON object; fix or delete it")


def write_config(data: dict) -> None:
    ensure_dirs()
    write_json(CONFIG, data)
