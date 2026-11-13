import json

import pytest

from agent_bridge import transcripts as tr
from agent_bridge.transcripts import Message


def omp(role, stop=None, text="", tool=False):
    content = ([{"type": "text", "text": text}] if text else []) + ([{"type": "toolCall", "name": "bash"}] if tool else [])
    return json.dumps({"type": "message", "message": {"role": role, "stopReason": stop, "content": content}})


def claude(kind, stop=None, text="", tool=False, **extra):
    content = ([{"type": "text", "text": text}] if text else []) + ([{"type": "tool_use", "name": "Bash"}] if tool else [])
    return json.dumps({"type": kind, "message": {"role": kind, "stop_reason": stop, "content": content}, **extra})


def write(path, *lines, end="\n"):
    with path.open("a") as f:
        f.write("\n".join(lines) + end)


def test_omp_transcript_is_parsed_and_other_entries_are_ignored(tmp_path):
    path = tmp_path / "s.jsonl"
    write(
        path,
        '{"type":"session","cwd":"/x"}',
        omp("user", text="do it"),
        omp("assistant", "toolUse", tool=True),
        "not json",
        "[1]",
        '{"type":"message","message":"oops"}',
    )
    assert tr.read_messages("omp", path) == [Message("user", None, "do it", False), Message("assistant", "toolUse", "", True)]


def test_the_model_of_each_reply_is_recorded(tmp_path):
    path = tmp_path / "s.jsonl"
    reply = {
        "type": "message",
        "message": {"role": "assistant", "stopReason": "stop", "provider": "openai-codex", "model": "gpt-6-luna", "content": []},
    }
    write(path, omp("user", text="hi"), json.dumps(reply))
    assert tr.last_model(tr.read_messages("omp", path)) == "openai-codex/gpt-6-luna"

    path = tmp_path / "c.jsonl"
    write(
        path,
        json.dumps(
            {
                "type": "assistant",
                "message": {"role": "assistant", "model": "claude-opus-5-5", "stop_reason": "end_turn", "content": []},
            }
        ),
    )
    assert tr.last_model(tr.read_messages("claude", path)) == "claude-opus-5-5"


def test_claude_sidechain_entries_are_not_the_agents_own_turn(tmp_path):
    path = tmp_path / "c.jsonl"
    write(
        path,
        claude("user", text="go"),
        claude("assistant", "end_turn", "subagent", isSidechain=True),
        claude("assistant", "end_turn", "done"),
    )
    assert [m.text for m in tr.read_messages("claude", path)] == ["go", "done"]


def test_a_line_still_being_written_is_not_read_yet(tmp_path):
    path = tmp_path / "s.jsonl"
    write(path, omp("user", text="one"))
    assert len(tr.read_messages("omp", path)) == 1
    half = omp("assistant", "stop", "two")
    write(path, half[:20], end="")  # a line still being written
    assert len(tr.read_messages("omp", path)) == 1
    write(path, half[20:])
    assert [m.text for m in tr.read_messages("omp", path)] == ["one", "two"]


A = "assistant"


@pytest.mark.parametrize(
    "engine,messages,quiet,expected",
    [
        ("omp", [], 0, "starting"),
        ("omp", [Message("user", None, "task", False)], 0, "working"),
        ("omp", [Message(A, "toolUse", "", True)], 0, "working"),
        ("omp", [Message("toolResult", None, "ok", False)], 0, "working"),
        ("omp", [Message(A, "stop", "## Report", False)], 0, "idle"),
        ("omp", [Message(A, "aborted", "", False)], 0, "idle"),
        ("omp", [Message(A, "error", "", False)], 5, "working"),  # omp is about to retry
        ("omp", [Message(A, "error", "", False)], 120, "idle"),  # retries exhausted
        ("claude", [Message("user", None, "task", False)], 0, "working"),
        ("claude", [Message(A, "tool_use", "", True)], 0, "working"),
        ("claude", [Message(A, "end_turn", "done", False)], 0, "idle"),
        ("claude", [Message(A, None, "streaming…", False)], 2, "working"),
        ("claude", [Message(A, None, "final text", False)], 60, "idle"),
        ("claude", [Message("user", None, "[Request interrupted by user for tool use]", False)], 0, "idle"),
    ],
)
def test_turn_state(engine, messages, quiet, expected):
    assert tr.turn_state(engine, messages, quiet) == expected


def test_last_assistant_text_skips_tool_only_messages_and_flags_failed_turns():
    messages = [Message(A, "stop", "the report", False), Message("user", None, "more", False), Message(A, "error", "", False)]
    assert tr.last_assistant_text(messages) == "the report"
    assert tr.last_turn_failed(messages)
    assert not tr.last_turn_failed(messages[:1])


def test_session_meta_recognises_both_engines_and_rejects_other_files(tmp_path):
    omp_file = tmp_path / "o.jsonl"
    write(omp_file, '{"type":"title","title":"Fix login"}', '{"type":"session","id":"abc","cwd":"/work/app"}')
    assert tr.session_meta(omp_file) == {"engine": "omp", "cwd": "/work/app", "title": "Fix login", "session_id": "abc"}

    claude_file = tmp_path / "c.jsonl"
    images_only = {
        "type": "user",
        "sessionId": "0123abcd-ef",
        "cwd": "/work/app",
        "message": {"role": "user", "content": [{"type": "image"}, {"type": "image"}]},
    }
    write(claude_file, json.dumps(images_only))
    assert tr.session_meta(claude_file) == {"engine": "claude", "cwd": "/work/app", "title": None, "session_id": "0123abcd-ef"}

    for content in ('{"type":"session","cwd":42}', '{"sessionId":"--dangerously-skip-permissions","cwd":"/x"}', "plain text", ""):
        other = tmp_path / "other.jsonl"
        other.write_text(content + "\n")
        assert tr.session_meta(other) is None
    assert tr.session_meta(tmp_path / "missing.jsonl") is None
