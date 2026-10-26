"""Mixed-source ordering, payload isolation, and cursor boundaries."""
import base64
import json

import pytest

from activity import ActivityHistory
from history import HistoryDB
from proxy_history import RequestHistory


@pytest.fixture
def timeline(tmp_path):
    db = HistoryDB(str(tmp_path / "history.sqlite"))
    proxy = RequestHistory(db)
    db.ensure_run("run", "managed", 1)
    db.set_model("run", "Native")
    with db.connect() as connection:
        for identifier, stamp, tokens, duration, state in (
                ("same", 10, 2, 1.0, "complete"), ("later", 12, 4, 2.0, "incomplete"),
                ("missing", 9, None, None, "running")):
            connection.execute("INSERT INTO generations(id, run_id, observed_at, model, state, "
                               "prompt_tokens, generated_tokens, total_seconds) VALUES(?, 'run', ?, 'Native', ?, ?, ?, ?)",
                               (identifier, stamp, state, tokens, tokens, duration))
    for identifier, stamp, tokens, duration, state in (
            ("same", 10, 2, 1.0, "complete"), ("between", 11, 3, 1.5, "cancelled"),
            ("no-usage", 8, None, None, "error")):
        metadata = {"id": identifier, "observed_at": stamp, "model": "Proxy", "state": state,
                    "endpoint": "/v1/chat/completions", "duration_seconds": duration,
                    "usage": {"prompt_tokens": tokens, "completion_tokens": tokens}, "run_id": "run"}
        proxy.begin(metadata)
        proxy.finish(metadata, "PRIVATE REQUEST", "PRIVATE RESPONSE")
    db.add_prompt_file("run", "/native/prompt.txt", 7, "PRIVATE NATIVE", False)
    return db, proxy, ActivityHistory(db)


@pytest.mark.parametrize("sort", ["time", "duration", "prompt_tokens", "generated_tokens"])
@pytest.mark.parametrize("order", ["asc", "desc"])
def test_mixed_paging_matches_global_order_with_ties_and_nulls(timeline, sort, order):
    _, _, activity = timeline
    expected = activity.list(sort=sort, order=order)["items"]
    actual, cursor = [], None
    while True:
        page = activity.list(sort=sort, order=order, limit=2, cursor=cursor)
        actual.extend(page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
        assert len(actual) <= len(expected)
    assert actual == expected
    assert len(actual) == len({row["id"] for row in actual}) == 7
    key = ActivityHistory.SORTS[sort]
    values = [row[key] for row in actual]
    present = [value for value in values if value is not None]
    assert values == sorted(present, reverse=order == "desc") + [None] * (len(values) - len(present))
    same = [row for row in actual if row["record_id"] == "same"]
    assert {row["source"] for row in same} == {"proxy", "monitored"}
    assert all("request_text" not in row and "response_text" not in row and "prompt_text" not in row for row in actual)


def test_source_filters_dates_prompts_and_models(timeline):
    _, _, activity = timeline
    assert len(activity.list(source="proxy")["items"]) == 3
    assert len(activity.list(source="monitored")["items"]) == 4
    assert len(activity.list(endpoint="/v1/chat/completions")["items"]) == 3
    assert activity.list(source="monitored", endpoint="/v1/chat/completions")["items"] == []
    assert len(activity.list(model="Native")["items"]) == 4
    assert len(activity.list(state="cancelled")["items"]) == 1
    assert len(activity.list(from_ts=10, to_ts=11)["items"]) == 2
    prompt = activity.list(state="prompt_only")["items"][0]
    assert prompt["has_prompt"] and prompt["record_id"].startswith("prompt:")
    assert activity.list(source="proxy")["models"] == ["Native", "Proxy"]
    assert len(activity.list(include_proxy=False)["items"]) == 4


@pytest.mark.parametrize("change", [{"source": "proxy"}, {"model": "Native"}, {"sort": "duration"},
                                     {"order": "asc"}, {"endpoint": "/completion"}, {"state": "complete"},
                                     {"from_ts": 1}, {"to_ts": 20}])
def test_cursors_are_bound_to_query(timeline, change):
    cursor = timeline[2].list(limit=1)["next_cursor"]
    with pytest.raises(ValueError, match="cursor"):
        timeline[2].list(cursor=cursor, **change)


@pytest.mark.parametrize("options", [{"source": "unknown"}, {"state": "unknown"}, {"sort": "unknown"},
                                      {"order": "unknown"}, {"limit": 0}, {"limit": 201},
                                      {"from_ts": float("nan")}, {"to_ts": float("inf")},
                                      {"cursor": "garbage"}])
def test_invalid_queries(timeline, options):
    with pytest.raises(ValueError):
        timeline[2].list(**options)


def test_bad_cursor_fields_and_version(timeline):
    activity = timeline[2]
    fields = json.loads(base64.urlsafe_b64decode(activity.list(limit=1)["next_cursor"]))
    for index, value in ((0, 2), (2, True), (3, "10"), (3, float("nan")), (4, "other"), (5, 42)):
        bad = list(fields)
        bad[index] = value
        with pytest.raises(ValueError, match="cursor"):
            activity.list(cursor=base64.urlsafe_b64encode(json.dumps(bad).encode()).decode())


def test_source_clears_preserve_other_source_and_legacy_contracts(timeline):
    db, proxy, activity = timeline
    assert all("source" not in row for row in db.list()["items"])
    assert all("source" not in row for row in proxy.list()["items"])
    proxy.clear()
    assert len(activity.list()["items"]) == 4
    assert activity.list(source="proxy")["items"] == []
    db.clear()
    assert activity.list()["items"] == []


def test_without_proxy_table(tmp_path):
    activity = ActivityHistory(HistoryDB(str(tmp_path / "native.sqlite")))
    assert activity.list(include_proxy=False)["items"] == []


def test_running_proxy_model_and_unreported_usage(timeline):
    _, proxy, activity = timeline
    proxy.begin({"id": "active", "observed_at": 13, "model": "Running model", "endpoint": "/completion"})
    running = activity.list(model="Running model")["items"][0]
    assert running["state"] == "running" and running["model"] == "Running model"
    assert running["prompt_tokens"] is None and running["generated_tokens"] is None
    assert "Running model" in activity.list()["models"]
    metadata = {"id": "active", "observed_at": 13, "state": "complete", "model": "Running model",
                "usage": {"prompt_tokens": "unknown", "completion_tokens": False}}
    proxy.finish(metadata, None, None)
    row = activity.list(model="Running model")["items"][0]
    assert row["prompt_tokens"] is None and row["generated_tokens"] is None
