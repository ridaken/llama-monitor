"""Usage accounting, persistence isolation, bounded storage, and failure behavior."""
import json
import sqlite3
import time

import pytest

from history import HistoryDB
import observability as obs


BASE = 1767225600.0


class Clock:
    def __init__(self, at=BASE):
        self.at, self.elapsed = at, 0

    def advance(self, seconds):
        self.at += seconds
        self.elapsed += seconds


def recording(tmp_path, clock=None, role="primary", kind="model", name="run"):
    clock = clock or Clock()
    recorder = obs.Recorder(str(tmp_path / "history.sqlite"), autostart=False)
    recorder.submit = recorder.storage.write
    tracker = obs.Tracker(recorder, name, role=role, kind=kind, model="model" if kind == "model" else "",
                          clock=lambda: clock.elapsed, wall=lambda: clock.at)
    return recorder, tracker, clock


def totals(recorder, clock, **filters):
    return recorder.storage.metrics(from_ts=BASE - 86400, to_ts=clock.at + 1, **filters)["totals"]


@pytest.mark.parametrize("role", ["primary", "embeddings"])
def test_overlapping_tasks_count_wall_time_once_and_unknown_is_separate(tmp_path, role):
    r, t, c = recording(tmp_path, role=role)
    c.advance(2)
    t.feed("server is listening")
    c.advance(1)
    t.feed("update_slots: all slots are idle")
    c.advance(3)
    t.feed("slot launch_slot: id 0 | task 1 | processing task 1")
    c.advance(2)
    t.feed("slot launch_slot: id 1 | task 2 | processing task 2")
    c.advance(3)
    t.feed("slot release: id 0 | task 1 | stop processing")
    c.advance(4)
    t.feed("slot release: id 1 | task 2 | stop processing")
    c.advance(2)
    t.finish("manual_stop", 0)
    result = totals(r, c, role=role)
    assert result["loading_seconds"] == 2
    assert result["load_seconds"] == 2
    assert result["unknown_seconds"] == 1
    assert result["idle_seconds"] == 5
    assert result["busy_seconds"] == 9
    assert result["process_uptime_seconds"] == 17
    assert result["utilization"] == pytest.approx(9 / 14)
    assert result["ready_outcomes"] == 1


def test_task_mismatch_missing_ready_and_failed_load(tmp_path):
    r, t, c = recording(tmp_path)
    c.advance(4)
    t.feed("slot launch_slot: id 0 | task 1 | processing task 1")
    c.advance(1)
    t.finish("unexpected_exit", 7)
    result = totals(r, c)
    assert result["loading_seconds"] == 5 and result["pre_ready_failures"] == 1
    assert result["unexpected_exits"] == 1 and "load_seconds" not in result


def test_mismatched_completion_does_not_claim_idle(tmp_path):
    r, t, c = recording(tmp_path)
    t.feed("all slots are idle")
    t.feed("slot launch_slot: id 0 | task 1 | processing task 1")
    c.advance(2)
    t.feed("slot release: id 0 | task 9 | stop processing")
    c.advance(3)
    t.finish("manual_stop")
    assert totals(r, c)["unknown_seconds"] == 3
    assert totals(r, c)["busy_seconds"] == 2


def test_unknown_task_requires_explicit_idle_to_restore_coverage(tmp_path):
    r, t, c = recording(tmp_path)
    t.feed("all slots are idle")
    t.feed("processing task without a slot")
    t.feed("slot launch_slot: id 0 | task 1 | processing task 1")
    c.advance(1)
    t.feed("slot release: id 0 | task 1 | stop processing")
    c.advance(2)
    t.feed("all slots are idle")
    c.advance(3)
    t.finish("manual_stop")
    result = totals(r, c)
    assert result["unknown_seconds"] == 2 and result["idle_seconds"] == 3
    assert result["busy_seconds"] == 1


def test_model_summaries_keep_roles_separate(tmp_path):
    r, t, c = recording(tmp_path)
    c.advance(2)
    t.finish("manual_stop")
    r, t, c = recording(tmp_path, role="embeddings", name="embedding")
    c.advance(3)
    t.finish("manual_stop")
    report = r.storage.metrics(from_ts=BASE, to_ts=BASE + 20)
    assert report["totals"]["process_uptime_seconds"] == 5
    assert {m["role"]: m["totals"]["process_uptime_seconds"] for m in report["models"]} == {"primary": 2, "embeddings": 3}


def test_sqlite_contention_drops_bounded_work_then_recovers(tmp_path):
    r = obs.Recorder(str(tmp_path / "history.sqlite"), autostart=False)
    lock = sqlite3.connect(r.storage.path)
    lock.execute("BEGIN IMMEDIATE")
    try:
        r.event("backend", "blocked", "Blocked.")
        r.start()
        deadline = time.monotonic() + 3
        while r.write_failures < 3 and time.monotonic() < deadline:
            time.sleep(.01)
        assert r.write_failures == 3 and r.dropped == 1
    finally:
        lock.rollback()
        lock.close()
    r.event("backend", "after_lock", "After lock.")
    deadline = time.monotonic() + 3
    while r.last_success is None and time.monotonic() < deadline:
        time.sleep(.01)
    r.close()
    assert r.last_success and r.error is None
    assert any(e["code"] == "persistence_recovered" for e in r.storage.events()["items"])


def test_checkpoint_retries_and_midnight_do_not_duplicate_daily_totals(tmp_path):
    c = Clock(BASE + 86398)
    r, t, c = recording(tmp_path, c)
    captured = []
    write = r.submit
    def capture(item):
        captured.append(item)
        write(item)
    r.submit = capture
    c.advance(5)
    t.checkpoint()
    r.storage.write(captured[-1])
    r.storage.write(captured[-1])
    result = r.storage.metrics(from_ts=BASE, to_ts=BASE + 86410)
    assert result["totals"]["loading_seconds"] == 5
    assert [row["values"]["loading_seconds"] for row in result["daily"]] == [2, 3]
    t.finish("manual_stop")


def test_clock_change_uses_monotonic_durations(tmp_path):
    r, t, c = recording(tmp_path)
    c.advance(2)
    c.at += 3600
    t.checkpoint()
    c.advance(3)
    t.finish("manual_stop")
    assert totals(r, c)["process_uptime_seconds"] == 5
    assert r.storage.events()["items"][0]["code"] == "clock_discontinuity"


def test_stop_latency_overlaps_lifetime_and_app_uptime_is_independent(tmp_path):
    r, t, c = recording(tmp_path)
    c.advance(2)
    t.stop_intent()
    c.advance(3)
    t.stop_intent(failed=True)
    c.advance(1)
    t.finish("manual_stop")
    result = totals(r, c)
    assert result["stop_seconds"] == 4
    assert result["stop_failures"] == 1
    assert result["process_uptime_seconds"] == 6
    r2, app, c2 = recording(tmp_path, kind="app", name="backend")
    c2.advance(7)
    app.finish("graceful_shutdown")
    assert totals(r2, c2)["app_uptime_seconds"] == 7
    assert totals(r2, c2)["process_uptime_seconds"] == 6


def test_stop_latency_includes_supervisor_observation_delay(tmp_path):
    r, t, c = recording(tmp_path)
    c.advance(3)
    t.stop_intent(requested_at=1)
    c.advance(2)
    t.finish("manual_stop")
    assert totals(r, c)["stop_seconds"] == 4


def test_reconciliation_preserves_final_checkpoint_published_during_probe(tmp_path, monkeypatch):
    r, t, c = recording(tmp_path)
    t.data["identity"] = {"pid": 123}
    c.advance(2)
    t.checkpoint()
    def finished_during_probe(_):
        t.finish("manual_stop", 15)
        return False
    monkeypatch.setattr("processes.identity_matches", finished_during_probe)
    obs.reconcile(r.storage)
    report = r.storage.metrics(from_ts=BASE, to_ts=BASE + 20)
    assert report["sessions"][0]["outcome"] == "manual_stop"
    assert report["sessions"][0]["exit_code"] == 15
    assert report["totals"].get("interruptions", 0) == 0


def test_run_rollups_keep_operation_associations_after_detail_expires(tmp_path, monkeypatch):
    r, t, c = recording(tmp_path)
    monkeypatch.setattr(obs.time, "time", lambda: c.at)
    r.measure("launch_attempts", model="model", role="primary", run_id="run")
    c.advance(2)
    t.checkpoint()
    with r.storage.connect() as db:
        db.execute("DELETE FROM app_sessions")
        r.storage._set_meta(db, "detail_floor", c.at)
    result = r.storage.metrics(run_id="run", from_ts=BASE, to_ts=BASE + 86400)
    assert result["totals"]["launch_attempts"] == 1


def test_additive_schema_clear_history_preserves_metrics_and_events(tmp_path):
    history = HistoryDB(str(tmp_path / "history.sqlite"))
    r, t, c = recording(tmp_path)
    r.event("backend", "starting", "Starting.")
    c.advance(2)
    t.finish("manual_stop")
    history.clear()
    obs.Storage(history.path)
    assert r.storage.events()["items"]
    assert totals(r, c)["loading_seconds"] == 2
    assert r.storage.metrics(run_id="expired")["available"] is False


def test_event_bounds_filters_and_forward_cursor_gap(tmp_path):
    r = obs.Recorder(str(tmp_path / "history.sqlite"), autostart=False)
    r.submit = r.storage.write
    r.event("launcher", "failed", "Fixed message.", "warning", role="primary", run_id="one", password="private")
    first = r.storage.events(direction="forward", level="warning", limit=1)
    assert len(first["items"]) == 1 and "password" not in first["items"][0]
    r.event("launcher", "failed", "Fixed message.", "error", role="embeddings", run_id="two")
    second = r.storage.events(direction="forward", level="warning", limit=1, cursor=first["next_cursor"])
    assert second["items"][0]["run_id"] == "two"
    assert r.storage.events(role="primary")["items"][0]["run_id"] == "one"
    assert len(r.storage.events(level="error")["items"]) == 1
    with pytest.raises(ValueError):
        r.storage.events(direction="forward", level="error", cursor=first["next_cursor"])
    with r.storage.connect() as db:
        db.execute("DELETE FROM app_events WHERE id=1")
        r.storage._set_meta(db, "event_floor", 2)
    assert r.storage.events(direction="forward", level="warning", cursor=first["next_cursor"])["gap"]


def test_oversized_event_and_failure_suppression(tmp_path):
    r = obs.Recorder(str(tmp_path / "history.sqlite"), autostart=False)
    r.submit = r.storage.write
    for _ in range(25):
        r.condition("history", "ingestion", True)
    r.condition("history", "ingestion", False)
    r.event("launcher", "context", "x" * 99999, **{k: "\U0001f999" * 9999 for k in obs.CONTEXT})
    events = r.storage.events()["items"]
    assert len(events) == 3
    assert all(len(json.dumps({k: v for k, v in e.items() if k != "id"}).encode()) <= 8192 for e in events)
    assert events[1]["suppressed_count"] == 24


def test_pruning_preserves_daily_totals_and_active_checkpoints(tmp_path):
    r, t, c = recording(tmp_path)
    c.advance(5)
    t.checkpoint()
    for _ in range(30):
        r.event("backend", "event", "x" * 500)
    r.storage.budget = 10000
    r.storage.prune()
    status = r.storage.status()
    assert status["estimated_retained_bytes"] <= 10000
    assert status["pruned_rows"] > 0 and status["producers"][0]["id"] == "run"
    t.finish("manual_stop")
    assert totals(r, c)["process_uptime_seconds"] == 5


def test_expired_detail_returns_bucket_boundaries_without_partial_bucket_totals(tmp_path):
    r, t, c = recording(tmp_path)
    c.advance(10)
    t.finish("manual_stop")
    with r.storage.connect() as db:
        db.execute("DELETE FROM usage_intervals")
        r.storage._set_meta(db, "detail_floor", c.at)
    full = r.storage.metrics(from_ts=BASE, to_ts=BASE + 86400)
    assert full["totals"]["process_uptime_seconds"] == 10
    partial = r.storage.metrics(from_ts=BASE + 1, to_ts=BASE + 20)
    assert partial["coverage"]["partial_daily_buckets_excluded_from_totals"]
    assert partial["daily"][0]["day"] == BASE
    assert partial["daily"][0]["values"]["process_uptime_seconds"] == 10
    assert partial["totals"].get("process_uptime_seconds", 0) == 0


def test_daily_bucket_pruning_cannot_be_resurrected_by_active_producer(tmp_path):
    r, t, c = recording(tmp_path)
    c.advance(4)
    t.checkpoint()
    with r.storage.connect() as db:
        db.execute("DELETE FROM usage_daily")
        r.storage._set_meta(db, "daily_floor", BASE + 86400)
    t.checkpoint()
    assert r.storage.metrics(from_ts=BASE, to_ts=BASE + 86400)["daily"] == []


def test_queue_overflow_and_storage_failure_are_bounded(tmp_path, monkeypatch):
    r = obs.Recorder(str(tmp_path / "history.sqlite"), autostart=False, queue_size=1)
    r.event("backend", "first", "First.")
    r.event("backend", "second", "Second.")
    assert r.health()["queue_drops"] == 1
    monkeypatch.setattr(r.storage, "write", lambda _: (_ for _ in ()).throw(sqlite3.OperationalError("private path")))
    start = time.monotonic()
    r.start()
    r.close()
    assert time.monotonic() - start < 2.5
    assert r.health()["write_failures"] and r.health()["queue_drops"] >= 2
    assert "private path" not in json.dumps(r.health())


def test_interrupted_producer_keeps_checkpoint_without_invented_exit(tmp_path, monkeypatch):
    r, t, c = recording(tmp_path)
    t.data["identity"] = {"pid": 123}
    c.advance(3)
    t.checkpoint()
    monkeypatch.setattr("processes.identity_matches", lambda _: False)
    monkeypatch.setattr(obs.time, "time", lambda: c.at + 5)
    obs.reconcile(r.storage)
    obs.reconcile(r.storage)
    result = r.storage.metrics(from_ts=BASE, to_ts=BASE + 20)
    session = result["sessions"][0]
    assert session["outcome"] == "interrupted" and session["unobserved_after"] == BASE + 3
    assert "ended_at" not in session
    assert result["totals"]["interruptions"] == 1
    assert result["totals"]["process_uptime_seconds"] == 3


@pytest.mark.parametrize("params", [{"level": "debug"}, {"role": "invalid"}, {"limit": 0},
                                    {"from_ts": float("nan")}, {"to_ts": float("inf")},
                                    {"from_ts": 5, "to_ts": 1}])
def test_query_validation(tmp_path, params):
    storage = obs.Storage(str(tmp_path / "history.sqlite"))
    with pytest.raises(ValueError):
        storage.events(**params)
    if set(params) <= {"from_ts", "to_ts", "role"}:
        with pytest.raises(ValueError):
            storage.metrics(**params)
