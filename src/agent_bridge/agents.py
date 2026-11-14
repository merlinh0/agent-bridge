"""What an agent is doing and what it changed, read from Tern, its session transcript and git.

Nothing here writes state or needs the MCP server, so the command line reads it too
(`agent-bridge status`, `agent-bridge diff`), cheaply enough for the Tern plugin to poll.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from . import checkouts, proc, store, tern, transcripts

STALL_SECONDS = 90  # a turn with no transcript activity for this long may be waiting on a dialog
STARTUP_SECONDS = 20  # no transcript this long after launch: probably a startup dialog
MAX_UNTRACKED_DIFFS = 200
BUSY = ("starting", "working")


def git_root(path: Path | str) -> Path | None:
    out = proc.git(path, "rev-parse", "--show-toplevel", check=False).strip()
    return Path(out) if out else None


@dataclass
class Observation:
    status: dict
    messages: list[transcripts.Message]
    transcript: Path | None


def process_state(t: dict, blocks: dict[int, dict]) -> str:
    """`live` only when the agent's own program is what runs in its block right now.

    A block that outlived its agent (the shell an adopted session was started from, say) is
    `exited`: typing a message there would run it as a shell command.
    """
    block = blocks.get(t.get("block"))
    if block is None:
        return "closed"
    if block.get("exited") is not None:
        return f"exited ({block['exited']})"
    if t["engine"] == "omp" and block.get("omp_session_file"):
        return "live"
    return "live" if tern.foreground(block["id"], block["window"])[:1] == [t["engine"]] else "exited"


def window(t: dict, blocks: dict[int, dict]) -> str | None:
    return (blocks.get(t.get("block")) or {}).get("window")


def transcript(t: dict, blocks: dict[int, dict]) -> Path | None:
    if t["engine"] == "omp":
        live = (blocks.get(t.get("block")) or {}).get("omp_session_file")
        if live and live.endswith(".jsonl"):
            return Path(live)  # follows /new and /resume typed in the pane
    if t.get("session_file"):
        return Path(t["session_file"])
    if t.get("session_dir"):
        return max(Path(t["session_dir"]).glob("*.jsonl"), key=transcripts.mtime, default=None)
    return transcripts.claude_transcript(t["session_id"]) if t.get("session_id") else None


def observe(t: dict, blocks: dict[int, dict]) -> Observation:
    """What an agent is doing right now. Reads only: polling must never write state."""
    process = process_state(t, blocks)
    path = transcript(t, blocks)
    messages: list[transcripts.Message] = []
    written_at = 0.0
    if path:
        try:
            messages = transcripts.read_messages(t["engine"], path)
            written_at = path.stat().st_mtime
        except OSError:
            path = None
    age = time.time() - written_at if path else None
    turn = transcripts.turn_state(t["engine"], messages, age or 0)
    if process != "live":
        state = process
    elif blocks[t["block"]].get("working"):
        state = "working"  # omp tells Tern when it is busy, retries after an error included
    elif turn == "idle" and written_at < t.get("sent_at", 0):
        state = "working"  # a message was sent and is not in the transcript yet
    else:
        state = turn
    status = {
        "name": t["name"],
        "engine": t["engine"],
        "model": transcripts.last_model(messages) or t.get("model") or "default",
        "thinking": t.get("thinking") or "default",
        "state": state,
        "seconds_since_activity": round(age) if age is not None else None,
        "branch": t.get("branch"),
        "cwd": t["cwd"],
    }
    if t.get("adopted"):
        status["adopted"] = True
    if state == "working" and age is not None and age > STALL_SECONDS:
        status["warning"] = "no transcript activity for a while: may be waiting on an approval or dialog (see capture_screen)"
    elif state == "starting" and time.time() - t.get("sent_at", t["created_at"]) > STARTUP_SECONDS:
        status["warning"] = "no transcript yet: check capture_screen (startup dialog, login, trust prompt?)"
    elif state not in BUSY and transcripts.last_turn_failed(messages):
        status["warning"] = "the last turn ended on an error or was aborted; the last message predates it (see capture_screen)"
    return Observation(status, messages, path)


def statuses(names: list[str] | None) -> list[dict]:
    blocks = tern.scan()
    tasks = store.all_tasks() if names is None else [store.load(n) for n in names]
    return [observe(t, blocks).status for t in tasks]


def report(t: dict, blocks: dict[int, dict], history: int = 0) -> dict:
    """Status, latest assistant message (normally the report) and git summary of one agent."""
    seen = observe(t, blocks)
    status = seen.status
    status["last_message"] = transcripts.last_assistant_text(seen.messages)
    if history > 0:
        texts = [m.text for m in seen.messages if m.role == "assistant" and m.text]
        status["history"] = texts[-min(history, 50) :]
    if t.get("base_sha"):
        cwd = t["cwd"]
        status["branch"] = proc.git(cwd, "rev-parse", "--abbrev-ref", "HEAD", check=False).strip() or t.get("branch")
        status["commits"] = proc.git(cwd, "log", "--oneline", f"{t['base_sha']}..HEAD", check=False).strip().splitlines()
        status["uncommitted"] = proc.git(cwd, "status", "--short", check=False).strip().splitlines()
        status["diffstat_vs_base"] = proc.git(cwd, "diff", "--shortstat", t["base_sha"], check=False).strip()
    if t.get("isolation"):
        status["merge"] = checkouts.bring_back_hint(t["isolation"], t["name"], t["branch"])
    return status


def diff(t: dict, path: str | None = None, stat_only: bool = False, max_chars: int = 80_000) -> str:
    """Everything the agent changed since its base commit: commits, uncommitted edits and new files."""
    if not t.get("base_sha"):
        raise ValueError(f"{t['name']} does not run in a git repository: no diff available")
    cwd = git_root(t["cwd"]) or t["cwd"]  # the whole repository, even for a session living in a subdirectory
    max_chars = max(1_000, min(max_chars, 400_000))
    spec = ["--", path] if path else []
    # No external diff drivers or textconv filters: the repository's config is not trusted to run commands here.
    plain = ("--no-ext-diff", "--no-textconv")
    parts = [proc.git(cwd, "diff", *plain, "--stat" if stat_only else "--patch", t["base_sha"], *spec, check=False)]
    untracked = [f for f in proc.git(cwd, "ls-files", "-z", "--others", "--exclude-standard", *spec).split("\0") if f]
    if stat_only:
        if untracked:
            parts.append("untracked: " + ", ".join(untracked))
    else:
        size = len(parts[0])
        for i, file in enumerate(untracked):
            if size > max_chars or i == MAX_UNTRACKED_DIFFS:
                parts.append(f"… {len(untracked) - i} more untracked file(s) not shown: " + ", ".join(untracked[i : i + 50]))
                break
            patch = proc.git(cwd, "diff", *plain, "--no-index", "--", "/dev/null", file, check=False)
            parts.append(patch or f"(new file {file}: could not be diffed, e.g. a nested repository)")
            size += len(parts[-1])
    out = "\n".join(p for p in parts if p.strip()) or "(no changes)"
    if len(out) > max_chars:
        out = out[:max_chars] + f"\n… truncated ({len(out)} chars total; narrow with path= or raise max_chars)"
    return out
