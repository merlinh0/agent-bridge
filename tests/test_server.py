"""The tools, against a real git repository and a fake Tern."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from conftest import git

from agent_bridge import server, store, tern
from agent_bridge import transcripts as tr


def say(agent: dict, role: str, stop: str | None = None, text: str = "") -> None:
    """Append a message to an omp agent's transcript."""
    directory = Path(agent["session_dir"])
    path = next(iter(directory.glob("*.jsonl")), directory / "session.jsonl")
    entry = {"type": "message", "message": {"role": role, "stopReason": stop, "content": [{"type": "text", "text": text}]}}
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def start(repo, **kwargs) -> dict:
    started = server.start_agent(repo=str(repo), task=kwargs.pop("task", "Fix the bug"), **kwargs)
    return store.load(started["name"])


def finish(agent: dict) -> None:
    say(agent, "user", text="Fix the bug")
    say(agent, "assistant", "stop", "## Report\ndone")


# ---------------------------------------------------------------- starting


def test_start_agent_creates_an_isolated_worktree_and_passes_the_task_as_one_argument(state, repo, fake_tern):
    task = "Fix it; $(touch /tmp/x) `id` --auto-approve\n-rf"
    agent = start(repo, task=task, name="fix", model="auto:fast")
    assert agent["branch"] == "agent/fix" and Path(agent["cwd"]).parent == store.WORKTREES
    assert git(agent["cwd"], "rev-parse", "--abbrev-ref", "HEAD").strip() == "agent/fix"
    argv = fake_tern.spawned[0]
    assert argv[-2:] == ["--", task]  # after `--`: never parsed as options
    assert argv[argv.index("--model") + 1] == "acme/small-1"
    assert "--auto-approve" not in argv[:-2]


def test_a_failed_launch_leaves_nothing_behind(state, repo, fake_tern, monkeypatch):
    def tern_is_closed(*args):
        raise RuntimeError("tern is not running")

    monkeypatch.setattr(tern, "spawn_tab", tern_is_closed)
    with pytest.raises(RuntimeError, match="not running"):
        start(repo, name="fix")
    assert store.all_tasks() == [] and not list(store.WORKTREES.iterdir())
    assert "agent/fix" not in git(repo, "branch", "--list")
    monkeypatch.setattr(tern, "spawn_tab", fake_tern.spawn_tab)
    assert start(repo, name="fix")["branch"] == "agent/fix"  # the same name works on retry


def test_auto_approve_needs_the_users_own_switch(state, repo, fake_tern):
    with pytest.raises(PermissionError, match="agent-bridge auto-approve on"):
        start(repo, auto_approve=True)
    assert fake_tern.spawned == [] and store.all_tasks() == []
    store.write_config({"allow_auto_approve": True})
    start(repo, auto_approve=True)
    assert "--auto-approve" in fake_tern.spawned[0]


# ---------------------------------------------------------------- model policy


def test_allowlist_is_enforced_and_fills_in_a_missing_model(state, repo, fake_tern):
    server.set_model_policy(allowed_models=["acme/*"])
    with pytest.raises(ValueError, match="not allowed"):
        start(repo, model="other/big-1")
    with pytest.raises(ValueError, match="not allowed"):
        start(repo, engine="claude", model="opus")
    assert start(repo)["model"] in ("acme/small-1", "acme/mid-1")  # never the unchecked engine default
    assert len(store.all_tasks()) == 1


def test_session_rules_override_the_users_defaults_without_touching_the_file(state):
    store.write_config({"allowed_models": ["acme/*"], "allow_auto_approve": True})
    assert server.get_model_policy() == {"allowed_models": ["acme/*"], "tiers": {}, "auto_approve_allowed": True}
    server.set_model_policy(allowed_models=["other/*"], tiers={"fast": "other/big-1"})
    assert server.get_model_policy()["allowed_models"] == ["other/*"]
    assert server.list_models()["tiers"]["fast"][0]["model"] == "other/big-1"
    assert store.read_config() == {"allowed_models": ["acme/*"], "allow_auto_approve": True}


def test_a_resume_is_checked_against_the_current_policy(state, repo, fake_tern):
    agent = start(repo, model="other/big-1")
    fake_tern.close(agent["block"], None)
    server.set_model_policy(allowed_models=["acme/*"])
    with pytest.raises(ValueError, match="not allowed"):
        server.send_message(agent["name"], "continue")
    assert len(fake_tern.spawned) == 1


def test_rules_that_could_never_apply_are_rejected(state):
    with pytest.raises(ValueError, match="matches no model"):
        server.set_model_policy(allowed_models=["nobody/*"])
    with pytest.raises(ValueError, match="unknown model"):
        server.set_model_policy(tiers={"fast": "gone/model"})
    assert server.get_model_policy()["allowed_models"] == []


# ---------------------------------------------------------------- state and messages


def test_agent_state_follows_the_transcript(state, repo, fake_tern):
    agent = start(repo)
    assert server.list_agents()[0]["state"] == "starting"
    say(agent, "user", text="Fix the bug")
    assert server.list_agents()[0]["state"] == "working"
    say(agent, "assistant", "stop", "## Report\ndone")
    report = server.get_agent(agent["name"])
    assert report["state"] == "idle" and report["last_message"] == "## Report\ndone"


def test_a_busy_pane_wins_over_a_transcript_that_looks_finished(state, repo, fake_tern):
    agent = start(repo)
    finish(agent)
    fake_tern.blocks[agent["block"]]["working"] = True  # e.g. omp retrying after an error
    assert server.list_agents()[0]["state"] == "working"


def test_reading_state_never_writes_it(state, repo, fake_tern):
    agent = start(repo)
    finish(agent)
    before = store.task_file(agent["name"]).read_text()
    server.list_agents()
    server.get_agent(agent["name"])
    assert store.task_file(agent["name"]).read_text() == before


def test_send_message_types_into_a_live_omp_agent(state, repo, fake_tern):
    agent = start(repo)
    finish(agent)
    server.send_message(agent["name"], "- also add a test")
    assert fake_tern.submitted == [(agent["block"], "- also add a test")]
    assert server.list_agents()[0]["state"] == "working"  # until the message shows up in the transcript


def test_send_message_does_not_type_while_a_startup_dialog_may_be_showing(state, repo, fake_tern):
    agent = start(repo)  # nothing recorded yet: a login or trust dialog may be on screen
    with pytest.raises(RuntimeError, match="may be showing a dialog"):
        server.send_message(agent["name"], "status?")
    assert fake_tern.submitted == []


def test_send_message_waits_for_claude_code_to_finish_its_turn(state, repo, fake_tern, tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "CLAUDE_PROJECTS", tmp_path / "claude")
    agent = start(repo, engine="claude")
    transcript = tmp_path / "claude" / "proj" / f"{agent['session_id']}.jsonl"
    transcript.parent.mkdir(parents=True)

    def entry(kind, stop, text):
        message = {"role": kind, "stop_reason": stop, "content": [{"type": "text", "text": text}]}
        with transcript.open("a") as f:
            f.write(json.dumps({"type": kind, "sessionId": agent["session_id"], "cwd": agent["cwd"], "message": message}) + "\n")

    entry("user", None, "Fix the bug")  # mid-turn: a permission dialog would take keystrokes as its answer
    with pytest.raises(RuntimeError, match="may be showing a dialog"):
        server.send_message(agent["name"], "2 more things")
    entry("assistant", "end_turn", "done")
    server.send_message(agent["name"], "2 more things")
    assert fake_tern.submitted == [(agent["block"], "2 more things")]


def test_an_exited_agent_is_resumed_in_a_new_tab_with_its_own_session(state, repo, fake_tern):
    agent = start(repo)
    finish(agent)
    fake_tern.close(agent["block"], None)
    assert server.list_agents()[0]["state"] == "closed"
    out = server.send_message(agent["name"], "one more thing")
    argv = fake_tern.spawned[-1]
    assert out["delivered"] == "resumed session in a new tab"
    assert argv[argv.index("--resume") + 1].endswith("session.jsonl") and argv[-1] == "one more thing"


def test_switching_model_restarts_the_agent_on_it(state, repo, fake_tern):
    agent = start(repo, model="acme/small-1")
    finish(agent)
    server.send_message(agent["name"], "think harder", model="other/big-1", thinking="high")
    argv = fake_tern.spawned[-1]
    assert fake_tern.closed == [agent["block"]]
    assert argv[argv.index("--model") + 1] == "other/big-1" and argv[argv.index("--thinking") + 1] == "high"
    with pytest.raises(ValueError, match="supports thinking"):
        server.send_message(agent["name"], "x", model="acme/small-1", thinking="medium")


def test_wait_returns_when_a_busy_agent_finishes_not_because_another_is_already_idle(state, repo, fake_tern, monkeypatch):
    monkeypatch.setattr(server, "POLL_SECONDS", 0.01)
    done, busy = start(repo, name="done"), start(repo, name="busy")
    finish(done)
    say(busy, "user", text="Fix the bug")

    async def scenario():
        waiting = asyncio.create_task(server.wait_for_agents(["done", "busy"], timeout_s=5))
        await asyncio.sleep(0.1)
        assert not waiting.done()  # "done" was idle from the start
        say(busy, "assistant", "stop", "finished")
        return await waiting

    result = asyncio.run(scenario())
    assert result["finished"] == ["busy"] and not result["timed_out"]
    assert asyncio.run(server.wait_for_agents(["done"], timeout_s=5)) == {
        "finished": [],
        "timed_out": False,
        "agents": server.list_agents()[:1],
    }


# ---------------------------------------------------------------- adopted sessions


@pytest.fixture
def adopted(state, repo, fake_tern, tmp_path, monkeypatch):
    """An omp session the user started from a shell pane, then adopted."""
    sessions = tmp_path / "omp-sessions"
    (sessions / "proj").mkdir(parents=True)
    monkeypatch.setattr(server, "OMP_SESSIONS", sessions)
    transcript = sessions / "proj" / "s.jsonl"
    reply = {
        "role": "assistant",
        "stopReason": "stop",
        "provider": "other",
        "model": "big-1",
        "content": [{"type": "text", "text": "hi"}],
    }
    transcript.write_text(
        json.dumps({"type": "session", "id": "s1", "cwd": str(repo)})
        + "\n"
        + json.dumps({"type": "message", "message": reply})
        + "\n"
    )
    block = fake_tern.add("omp", cwd=str(repo), omp_session_file=str(transcript), title="my session")
    name = server.adopt_session(block=block)["name"]
    return store.load(name), transcript


def test_adopting_a_live_pane_drives_it_in_place(adopted, fake_tern):
    agent, transcript = adopted
    assert agent["adopted"] and agent["session_file"] == str(transcript) and fake_tern.spawned == []
    with pytest.raises(ValueError, match="already managed"):
        server.adopt_session(session_file=str(transcript))


def test_an_adopted_session_reports_its_real_model_and_branch(adopted, repo):
    agent, _ = adopted
    git(repo, "checkout", "-q", "-b", "docs/readme")
    status = server.get_agent(agent["name"])
    assert status["model"] == "other/big-1" and status["branch"] == "docs/readme"


def test_adopting_a_session_on_a_disallowed_model_warns(state, repo, fake_tern, tmp_path, monkeypatch):
    sessions = tmp_path / "omp-sessions"
    (sessions / "proj").mkdir(parents=True)
    monkeypatch.setattr(server, "OMP_SESSIONS", sessions)
    transcript = sessions / "proj" / "s.jsonl"
    reply = {"role": "assistant", "stopReason": "stop", "provider": "other", "model": "big-1", "content": []}
    transcript.write_text(
        json.dumps({"type": "session", "id": "s1", "cwd": str(repo)})
        + "\n"
        + json.dumps({"type": "message", "message": reply})
        + "\n"
    )
    block = fake_tern.add("omp", cwd=str(repo), omp_session_file=str(transcript))
    server.set_model_policy(allowed_models=["acme/*"])
    assert "does not allow" in server.adopt_session(block=block)["warning"]


def test_nothing_is_typed_into_the_shell_an_adopted_agent_left_behind(adopted, fake_tern):
    agent, transcript = adopted
    pane = agent["block"]
    fake_tern.programs[pane] = "zsh"  # omp exited: the pane is the user's shell again
    fake_tern.blocks[pane]["omp_session_file"] = None
    assert server.list_agents()[0]["state"] == "exited"
    with pytest.raises(RuntimeError):
        server.press_keys(agent["name"], ["Enter"])
    with pytest.raises(RuntimeError):
        server.capture_screen(agent["name"])
    server.send_message(agent["name"], "rm -rf dist # then continue")
    assert fake_tern.submitted == [] and fake_tern.pressed == []
    assert fake_tern.closed == [] and pane in fake_tern.blocks  # the user's pane is left alone
    assert fake_tern.spawned[-1][-3:-1] == [str(transcript), "--"]  # resumed in a tab of its own


def test_stopping_an_adopted_session_deletes_nothing(adopted, fake_tern, repo):
    agent, transcript = adopted
    (repo / "wip.txt").write_text("unsaved")
    out = server.stop_agent(agent["name"], cleanup=True, force=True)
    assert out == {"name": agent["name"], "forgotten": True, "tab": "left open"}
    assert (repo / "wip.txt").exists() and transcript.exists() and agent["block"] in fake_tern.blocks


@pytest.mark.parametrize("make", ["outside", "not_jsonl", "not_a_transcript", "missing"])
def test_only_real_transcripts_from_the_session_stores_can_be_adopted(state, fake_tern, tmp_path, monkeypatch, make):
    sessions = tmp_path / "omp-sessions"
    (sessions / "proj").mkdir(parents=True)
    monkeypatch.setattr(server, "OMP_SESSIONS", sessions)
    header = json.dumps({"type": "session", "id": "s1", "cwd": str(tmp_path)}) + "\n"
    path = {
        "outside": tmp_path / "evil.jsonl",
        "not_jsonl": sessions / "proj" / "s.json",
        "not_a_transcript": sessions / "proj" / "notes.jsonl",
        "missing": sessions / "proj" / "gone.jsonl",
    }[make]
    if make != "missing":
        path.write_text("hello\n" if make == "not_a_transcript" else header)
    with pytest.raises(ValueError, match="not a saved omp / Claude Code session"):
        server.adopt_session(session_file=str(path))
    assert fake_tern.spawned == []


# ---------------------------------------------------------------- diff and cleanup


def test_get_diff_shows_commits_edits_and_new_files(state, repo, fake_tern):
    agent = start(repo)
    work = Path(agent["cwd"])
    (work / "app.py").write_text("print('fixed')\n")
    git(work, "commit", "-qam", "fix")
    (work / "app.py").write_text("print('fixed again')\n")
    (work / "café.md").write_text("new notes\n")
    diff = server.get_diff(agent["name"])
    assert "+print('fixed again')" in diff and "+new notes" in diff
    assert "café.md" in server.get_diff(agent["name"], stat_only=True)
    assert "new notes" not in server.get_diff(agent["name"], path="app.py")


def test_get_diff_ignores_diff_drivers_configured_in_the_repository(state, repo, fake_tern, tmp_path):
    agent = start(repo)
    work = Path(agent["cwd"])
    marker = tmp_path / "ran"
    git(work, "config", "diff.external", f"touch {marker} #")
    (work / "app.py").write_text("print('changed')\n")
    assert "+print('changed')" in server.get_diff(agent["name"])
    assert not marker.exists()


def unsaved(kind: str, work: Path) -> None:
    if kind == "untracked":
        (work / "notes.txt").write_text("x")
    elif kind == "edited":
        (work / "app.py").write_text("edited\n")
    else:
        if kind == "detached":
            git(work, "checkout", "-q", "--detach")
        elif kind == "other-branch":
            git(work, "checkout", "-q", "-b", "elsewhere")
        (work / "app.py").write_text("committed\n")
        git(work, "commit", "-qam", "work")
        if kind == "left-branch":  # the work stays on the agent's branch, HEAD moved back off it
            git(work, "checkout", "-q", "--detach", "HEAD~1")


@pytest.mark.parametrize("kind", ["untracked", "edited", "committed", "detached", "other-branch", "left-branch"])
def test_cleanup_refuses_to_lose_work(state, repo, fake_tern, kind):
    agent = start(repo, name="fix")
    unsaved(kind, Path(agent["cwd"]))
    with pytest.raises(RuntimeError, match="refusing cleanup"):
        server.stop_agent("fix", cleanup=True)
    assert Path(agent["cwd"]).exists() and store.load("fix")
    assert agent["block"] in fake_tern.blocks  # refused before the tab was closed


def test_cleanup_refuses_when_the_worktree_cannot_be_checked(state, repo, fake_tern):
    agent = start(repo, name="fix")
    (Path(agent["cwd"]) / ".git").write_text("gitdir: /nowhere\n")
    with pytest.raises(RuntimeError, match="could not be checked"):
        server.stop_agent("fix", cleanup=True)
    assert Path(agent["cwd"]).exists()


def test_cleanup_removes_merged_work_and_force_discards_the_rest(state, repo, fake_tern):
    merged = start(repo, name="merged")
    work = Path(merged["cwd"])
    (work / "app.py").write_text("merged\n")
    git(work, "commit", "-qam", "work")
    git(repo, "merge", "-q", "agent/merged")
    out = server.stop_agent("merged", cleanup=True)
    assert out["checkout"] == "removed" and not work.exists()
    assert "agent/merged" not in git(repo, "branch", "--list")

    dropped = start(repo, name="dropped")
    unsaved("committed", Path(dropped["cwd"]))
    server.stop_agent("dropped", cleanup=True, force=True)
    assert not Path(dropped["cwd"]).exists() and "agent/dropped" not in git(repo, "branch", "--list")
    assert store.all_tasks() == []


def test_a_tampered_record_cannot_point_cleanup_at_another_directory(state, repo, fake_tern, tmp_path):
    agent = start(repo, name="fix")
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "file").write_text("keep")
    agent.update(cwd=str(precious), session_dir=str(precious))
    store.save(agent)
    server.stop_agent("fix", cleanup=True, force=True)
    assert (precious / "file").exists()


# ---------------------------------------------------------------- terminal input


@pytest.mark.parametrize("keys", [["--window", "3", "Enter"], ["-x"], [""], ["a b"], []])
def test_keys_that_tern_could_read_as_options_are_refused(keys, monkeypatch):
    monkeypatch.setattr(tern, "tern", lambda *a, **k: pytest.fail("tern must not be called"))
    with pytest.raises(ValueError):
        tern.press(1, None, keys)


def test_text_is_sent_after_the_option_separator(monkeypatch):
    calls = []
    monkeypatch.setattr(tern, "tern", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    tern.submit(7, None, "--help me")
    assert calls[0] == ("send", "7", "paste", "--", "--help me")
