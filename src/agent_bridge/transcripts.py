"""Read omp and Claude Code session transcripts (JSONL) and tell whether a turn is over.

The transcript is the only stable, engine-independent record of what an agent did, so agent
state is derived from it rather than from the terminal screen.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

CLAUDE_PROJECTS = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"

_TAIL_BYTES = 4_000_000  # a long session is only read from its tail (about 15 ms to parse)
_HEAD_LINES = 40
# Claude Code logs streamed assistant entries with no stop reason; one that stays last this long is final.
_CLAUDE_SETTLE_SECONDS = 15
_CLAUDE_TURN_ENDED = ("[Request interrupted by user", "<local-command-stdout>", "<local-command-stderr>")
_OMP_RETRY_SECONDS = 30
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{7,63}")  # passed to `claude --resume`


@dataclass(frozen=True)
class Message:
    role: str | None
    stop: str | None
    text: str
    tool_use: bool
    model: str | None = None  # the model that wrote an assistant message, as the engine recorded it


def _parse(engine: str, line: bytes) -> Message | None:
    try:
        entry = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(entry, dict):
        return None
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    if engine == "omp":
        if entry.get("type") != "message":
            return None
        stop = message.get("stopReason")
        provider, model_id = message.get("provider"), message.get("model")
        model = f"{provider}/{model_id}" if isinstance(provider, str) and isinstance(model_id, str) else None
    else:
        if entry.get("type") not in ("user", "assistant") or entry.get("isSidechain"):
            return None
        stop = message.get("stop_reason")
        model = _text(message.get("model"))
    content = message.get("content") or []
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    parts = [c for c in content if isinstance(c, dict)]
    text = "\n".join(c.get("text") or "" for c in parts if c.get("type") == "text")
    tool_use = any(c.get("type") in ("toolCall", "tool_use") for c in parts)
    role = message.get("role")
    return Message(role=role, stop=stop, text=text.strip(), tool_use=tool_use, model=model if role == "assistant" else None)


def read_messages(engine: str, path: Path) -> list[Message]:
    """The transcript's recent messages, oldest first.

    A line cut by the tail window, or still being written, does not parse and is skipped.
    """
    with path.open("rb") as f:
        f.seek(max(0, path.stat().st_size - _TAIL_BYTES))
        lines = f.read().splitlines()
    return [m for m in (_parse(engine, line) for line in lines) if m]


def turn_state(engine: str, messages: list[Message], seconds_since_write: float) -> str:
    """`starting` (nothing recorded yet), `working`, or `idle` (the last turn has ended)."""
    if not messages:
        return "starting"
    last = messages[-1]
    if engine == "claude" and last.role == "user" and last.text.startswith(_CLAUDE_TURN_ENDED):
        return "idle"  # an interrupted turn or a local command ends on a user entry
    if last.role != "assistant" or last.tool_use:
        return "working"  # a prompt or tool result is waiting for the model, or a tool is running
    if engine == "omp":
        if last.stop == "error":
            # omp retries a failed request by itself: only an error that stays last is final.
            return "idle" if seconds_since_write > _OMP_RETRY_SECONDS else "working"
        return "working" if last.stop in (None, "toolUse") else "idle"
    if last.stop in ("end_turn", "stop_sequence", "max_tokens"):
        return "idle"
    return "idle" if last.stop is None and seconds_since_write > _CLAUDE_SETTLE_SECONDS else "working"


def last_turn_failed(messages: list[Message]) -> bool:
    """The last turn ended on an error or was aborted, so the last assistant text predates it."""
    return bool(messages) and messages[-1].role == "assistant" and messages[-1].stop in ("error", "aborted")


def last_model(messages: list[Message]) -> str | None:
    """The model the session actually ran on most recently."""
    return next((m.model for m in reversed(messages) if m.model), None)


def last_assistant_text(messages: list[Message]) -> str:
    return next((m.text for m in reversed(messages) if m.role == "assistant" and m.text), "")


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _head(path: Path) -> list[dict]:
    entries = []
    with path.open("rb") as f:
        for _ in range(_HEAD_LINES):
            line = f.readline()
            if not line:
                break
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(entry, dict):
                entries.append(entry)
    return entries


def session_meta(path: Path) -> dict | None:
    """{engine, cwd, title, session_id} of a saved transcript, or None if the file is not one."""
    try:
        entries = _head(path)
    except OSError:
        return None
    header = next((e for e in entries if e.get("type") == "session" and _text(e.get("cwd"))), None)
    if header:
        title = next((_text(e.get("title")) for e in entries if e.get("type") == "title"), None)
        return {"engine": "omp", "cwd": header["cwd"], "title": title, "session_id": _text(header.get("id"))}
    first = next((e for e in entries if _SESSION_ID.fullmatch(_text(e.get("sessionId")) or "") and _text(e.get("cwd"))), None)
    if first:
        prompt = next((e for e in entries if e.get("type") == "user" and not e.get("isMeta")), {})
        message = prompt.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            content = " ".join(_text(c.get("text")) or "" for c in content if isinstance(c, dict))
        lines = content.strip().splitlines() if isinstance(content, str) else []
        return {
            "engine": "claude",
            "cwd": first["cwd"],
            "title": lines[0][:80] if lines else None,
            "session_id": first["sessionId"],
        }
    return None


def claude_transcript(session_id: str) -> Path | None:
    return next(iter(CLAUDE_PROJECTS.glob(f"*/{session_id}.jsonl")), None)


def mtime(path: Path) -> float:
    """Modification time, 0 if the file has just disappeared."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def claude_transcripts(cwd: str) -> list[Path]:
    """Claude Code transcripts of a directory, most recently written first.

    Claude Code does not tell the terminal which session a pane runs, so the caller can only
    guess from this list which one belongs to a live pane.
    """
    files: list[Path] = []
    for directory in {cwd, os.path.realpath(cwd)}:
        project = CLAUDE_PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", directory)
        if project.is_dir():
            files.extend(project.glob("*.jsonl"))
    return sorted(files, key=mtime, reverse=True)
