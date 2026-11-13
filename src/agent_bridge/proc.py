"""Subprocess helpers shared by the git, Tern and omp wrappers."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def find_executable(name: str, *candidates: str | Path | None) -> str | None:
    """First executable among `candidates`, else `name` on PATH.

    GUI apps such as Claude Desktop start MCP servers with a minimal PATH, so well-known install
    locations are checked before giving up.
    """
    for candidate in candidates:
        if candidate and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(name)


def run(
    args: list[str], *, cwd: Path | str | None = None, check: bool = True, timeout: float = 60, env: dict[str, str] | None = None
) -> str:
    """Run a command without a shell and return its stdout.

    Raises RuntimeError (never a raw subprocess exception) so tool callers get one readable line.
    """
    label = " ".join(args[:3])
    try:
        p = subprocess.run(
            args, check=False, cwd=cwd, env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"`{label} …` timed out after {timeout:.0f}s") from e
    except OSError as e:
        raise RuntimeError(f"could not run `{label} …`: {e}") from e
    if check and p.returncode != 0:
        raise RuntimeError(f"`{label} …` failed ({p.returncode}): {(p.stderr or p.stdout).strip()}")
    return p.stdout


def git(cwd: Path | str, *args: str, check: bool = True, timeout: float = 60) -> str:
    # fsmonitor is a configurable command: never let a checkout an agent can write to run one here.
    return run(["git", "-C", str(cwd), "-c", "core.fsmonitor=false", *args], check=check, timeout=timeout)


def commit(cwd: Path | str, rev: str) -> str:
    """Full id of the commit `rev` names, or RuntimeError."""
    return git(cwd, "rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}").strip()
