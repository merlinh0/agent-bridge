"""The isolated copy of a repository each agent works in.

Two backends, same contract: the copy starts at a given commit on its own branch `agent/<name>`,
and work leaves it only through git commits.

- ovnfs (https://github.com/stencil-hq/ovnfs), used when it is installed: a copy-on-write checkout
  that opens in about a second and still shows the files git ignores (node_modules, .env, build
  caches), so the agent needs no reinstall. Its commits stay in a private git directory until
  `ovnfs apply <checkout> --branch agent/<name>` brings them into the repository.
- A git worktree otherwise: a full checkout under ~/.agent-bridge/worktrees whose branch lives in
  the repository from the start, but without the ignored files.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import proc, store

OVNFS = proc.find_executable("ovnfs", os.environ.get("AGENT_BRIDGE_OVNFS"), Path.home() / ".local/bin/ovnfs")
# AGENT_BRIDGE_ISOLATION=worktree keeps git worktrees even where ovnfs is installed.
BACKEND = "ovnfs" if OVNFS and os.environ.get("AGENT_BRIDGE_ISOLATION") != "worktree" else "worktree"
CHECKOUT_TIMEOUT_SECONDS = 600  # a git worktree of a large repository


def _ovnfs(*args: str) -> str:
    # On macOS ovnfs mounts its checkouts with mount_nfs from /sbin, which GUI apps leave off PATH.
    path = os.pathsep.join([os.environ.get("PATH", ""), "/usr/sbin", "/sbin"])
    return proc.run([OVNFS, *args], env={**os.environ, "PATH": path})


def ovnfs_name(agent: str) -> str:
    """The agent's checkout name in ovnfs, derived from its validated name so a record cannot point elsewhere."""
    return f"agent-bridge-{agent}"


def create(repo: Path, name: str, base_sha: str) -> tuple[str, Path, str]:
    """(backend, path, branch) of a new isolated copy of `repo` at `base_sha` on branch agent/<name>."""
    branch = f"agent/{name}"
    if BACKEND == "ovnfs":
        reply = json.loads(_ovnfs("new", str(repo), "--rev", base_sha, "--name", ovnfs_name(name), "--json"))
        path = Path(reply["checkout"]["path"])
        try:
            proc.git(path, "switch", "-q", "-c", branch)
        except RuntimeError:
            remove("ovnfs", repo, path, name, branch, force=True)
            raise
        return "ovnfs", path, branch
    path = store.WORKTREES / f"{repo.name}-{name}"
    proc.git(repo, "worktree", "add", "-b", branch, str(path), base_sha, timeout=CHECKOUT_TIMEOUT_SECONDS)
    return "worktree", path, branch


def unsaved_work(repo: str, path: str, branch: str) -> str | None:
    """Why removing the copy would lose work, or None when it would not. Whatever cannot be checked counts as unsaved."""
    try:
        dirty = proc.git(path, "status", "--porcelain").strip().splitlines()
        merged_into = proc.commit(repo, "HEAD")
        # Commits on the copy's HEAD or on its branch that the repository's HEAD does not have: the
        # agent may have detached HEAD or switched branch. Counted from the copy, which sees the
        # repository's commits in both backends.
        ahead = proc.git(path, "rev-list", "--count", "HEAD", f"refs/heads/{branch}", "--not", merged_into).strip()
    except RuntimeError:
        return f"its checkout could not be checked (inspect {path})"
    reasons = []
    if dirty:
        reasons.append(f"{len(dirty)} uncommitted or untracked file(s) ({', '.join(line.strip() for line in dirty[:5])})")
    if ahead != "0":
        reasons.append(f"{ahead} commit(s) not merged into the repository's HEAD")
    return " and ".join(reasons) or None


def bring_back_hint(backend: str, name: str, branch: str) -> str:
    if backend == "ovnfs":
        return (
            f"bring the commits into the repository with `ovnfs apply {ovnfs_name(name)} --branch {branch}`, then merge {branch}"
        )
    return f"merge {branch}"


def remove(backend: str, repo: Path | str, path: Path | str, name: str, branch: str, force: bool) -> None:
    """Delete the copy and its branch. Without `force`, git refuses a branch that is not merged."""
    if backend == "ovnfs":
        # The branch exists in the repository only if `ovnfs apply --branch` brought it there.
        if proc.git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).strip():
            proc.git(repo, "branch", "-D" if force else "-d", branch)
        _ovnfs("rm", ovnfs_name(name))
        return
    # Without force, git itself refuses a worktree with changes.
    proc.git(repo, "worktree", "remove", *(["--force"] if force else []), str(path))
    proc.git(repo, "branch", "-D" if force else "-d", branch, check=not force)
