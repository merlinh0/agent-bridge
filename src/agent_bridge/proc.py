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


def run(args: list[str], *, cwd: Path | str | None = None, check: bool = True, timeout: float = 60) -> str:
    """Run a command without a shell and return its stdout.

    Raises RuntimeError (never a raw subprocess exception) so tool callers get one readable line.
    """
    label = " ".join(args[:3])
    try:
        p = subprocess.run(args, check=False, cwd=cwd, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"`{label} …` timed out after {timeout:.0f}s") from e
    except OSError as e:
        raise RuntimeError(f"could not run `{label} …`: {e}") from e
    if check and p.returncode != 0:
        raise RuntimeError(f"`{label} …` failed ({p.returncode}): {(p.stderr or p.stdout).strip()}")
    return p.stdout
