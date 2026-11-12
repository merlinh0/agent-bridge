import json
import stat

import pytest

from agent_bridge import store


@pytest.mark.parametrize("name", ["../config", "/etc/passwd", "a/b", "..", "", "Caps", "a b", "-x", "x" * 49])
def test_names_that_could_leave_the_state_directory_are_rejected(state, name):
    with pytest.raises(ValueError):
        store.load(name)
    with pytest.raises(ValueError):
        store.forget(name)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Fix the CSV export (urgent!)", "fix-the-csv-export-urgent"),
        ("../../etc", "etc"),
        ("éàü", "agent"),
        ("x" * 200, "x" * 48),
    ],
)
def test_slug_always_yields_a_valid_name(state, text, expected):
    assert store.slug(text) == expected
    store.task_file(store.slug(text))  # does not raise


def test_unique_name_avoids_existing_agents(state):
    store.save({"name": "fix", "created_at": 1})
    assert store.unique_name("fix") == "fix-2"


def test_state_is_private_and_writes_keep_file_permissions(state):
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    store.save({"name": "a", "created_at": 1})
    assert stat.S_IMODE(store.task_file("a").stat().st_mode) == 0o600
    shared = state / "shared.json"
    shared.write_text("{}")
    shared.chmod(0o644)
    store.write_json(shared, {"k": 1})
    assert stat.S_IMODE(shared.stat().st_mode) == 0o644
    assert json.loads(shared.read_text()) == {"k": 1}
    assert not list(state.glob(".shared.json.*"))  # no temporary file left behind


def test_a_corrupt_agent_record_does_not_hide_the_others(state):
    store.save({"name": "good", "created_at": 1})
    (store.TASKS / "bad.json").write_text("{not json")
    assert [t["name"] for t in store.all_tasks()] == ["good"]


@pytest.mark.parametrize("content", ["{not json", "[]", '"text"'])
def test_a_broken_policy_file_is_an_error_not_an_empty_policy(state, content):
    store.CONFIG.write_text(content)
    with pytest.raises(RuntimeError):
        store.read_config()
