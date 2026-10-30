"""Exercise capture, retention, and history together using real CPU-only children."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest

import log_capture
import managed_logs as logs
import store
from history import HistoryDB, LogFollower


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "HOME_DIR", str(tmp_path))
    monkeypatch.setattr(store, "MANAGED_LOG", str(tmp_path / "llama-server.log"))
    monkeypatch.setattr(store, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("LLAMA_MONITOR_DATA_DIR", str(tmp_path))
    return tmp_path


def writer(**kwargs):
    return logs.Writer(uuid.uuid4().hex, "Test model", **kwargs)


def emit(capture, message, level="debug"):
    capture.feed(json.dumps({"type": "log", "level": level, "msg": message, "time": 42}))


def test_filter_saves_summaries_failures_and_continuations(isolated):
    capture = writer()
    emit(capture, "print_info: n_layer = 1")
    emit(capture, "load_tensors: layer 0 assigned to device CPU")
    emit(capture, "server is listening")
    for _ in range(2000):
        emit(capture, "que start_loop: waiting for new tasks")
    emit(capture, "slot launch_slot: id 0 | task 7 | processing task")
    emit(capture, "slot print_timing: id 0 | task 7 | total time = 30.0 ms / 4 tokens")
    emit(capture, "slot print_timing: id 0 | task 7 | n_gen = 168, tg = 22.56 t/s")
    emit(capture, "unknown debugging detail")
    emit(capture, "normal message from a future server", "info")
    emit(capture, "CUDA allocation failed", "error")
    capture.feed("    driver failure continuation\n")
    emit(capture, "prompt: secret payload", "info")
    emit(capture, "token piece: secret token", "debug")
    content = logs.console(capture.run_id)["content"]
    assert "waiting for new tasks" not in content
    assert "n_gen" not in content
    assert "unknown debugging" not in content and "secret" not in content
    assert "normal message" in content and "CUDA allocation failed" in content
    assert "driver failure continuation" in content and "total time" in content
    assert "assigned to device CPU" in content


def test_rotation_history_and_metadata_resume(isolated):
    capture = writer(segment_size=400)
    emit(capture, "common_init: fitting params to device memory")
    emit(capture, "load_tensors: CPU model buffer size = 999.0 MiB")
    emit(capture, "common_fit_params: fitting params to free memory took 0.1 seconds")
    emit(capture, "print_info: n_layer = 1")
    emit(capture, "load_tensors: layer 0 assigned to device CPU")
    emit(capture, "load_tensors: layer 1 assigned to device CPU")
    emit(capture, "load_tensors: CPU model buffer size = 2.0 MiB")
    emit(capture, "server is listening")
    db = HistoryDB(str(isolated / "history.sqlite"))
    follower = LogFollower(db, store.MANAGED_LOG, run_id=capture.run_id, managed=True)
    emit(capture, "slot launch_slot: id 0 | task 7 | processing task")
    follower.poll()
    # Resume a partially ingested request after a dashboard restart.
    follower = LogFollower(db, store.MANAGED_LOG, run_id=capture.run_id, managed=True)
    for msg in ("prompt eval time = 100.0 ms / 2 tokens", "eval time = 200.0 ms / 3 tokens",
                "total time = 300.0 ms / 5 tokens", "draft acceptance = 0.5 ( 2 accepted / 4 generated)"):
        emit(capture, "slot print_timing: id 0 | task 7 | " + msg)
    capture.finish(0, "manual_stop")
    follower.finish_segment()
    rows = db.latest_by_slot(capture.run_id)
    assert rows[0]["state"] == "complete" and rows[0]["generated_tokens"] == 3
    assert rows[0]["prompt_tokens"] == 2
    assert follower.memory.totals == {"CPU": 2.0}
    assert follower.placement.snapshot()["total_layers"] == 1
    assert len(logs.segments(capture.run_id)) > 1
    assert not follower.gap
    before = follower.offset
    follower.poll()
    assert follower.offset == before and len(db.latest_by_slot(capture.run_id)) == 1


def test_budget_evicts_completed_runs_then_current_segments(isolated):
    old = writer(budget=16000, segment_size=1000)
    old.finish(0, "manual_stop")
    current = writer(budget=16000, segment_size=1000)
    emit(current, "print_info: n_layer = 1")
    emit(current, "load_tensors: layer 0 assigned to device CPU")
    emit(current, "load_tensors: layer 1 assigned to device CPU")
    emit(current, "server is listening")
    cursor = logs.console(current.run_id)["cursor"]
    for _ in range(100):
        emit(current, "Something went wrong " + "x" * 200, "error")
    assert not logs.load(old.run_id)
    assert sum(p.stat().st_size for p in logs.root().rglob("*") if p.is_file()) <= 16000
    record = logs.load(current.run_id)
    assert record["gap"]
    assert record["metadata"]["placement"]["blocks"][0]["layers"]["0"] == "CPU"
    assert logs.console(current.run_id, cursor)["reset"]
    db = HistoryDB(str(isolated / "history.sqlite"))
    follower = LogFollower(db, store.MANAGED_LOG, run_id=current.run_id, managed=True)
    follower.poll()
    assert follower.gap and follower.placement.snapshot()["status"] == "available"


def test_oversized_and_partial_final_lines_are_bounded(isolated):
    result = list(logs.bounded_lines(io.BytesIO(b"x" * (logs.MAX_LINE * 4) + b"\nlast error")))
    assert len(result) == 2 and len(result[0][0]) == logs.MAX_LINE and result[0][1]
    assert result[1] == (b"last error", False)
    capture = writer()
    capture.feed("server is listening")
    for _ in range(10000):
        emit(capture, "que start_loop: waiting for new tasks")
    assert sum(p.stat().st_size for p in logs.segments(capture.run_id)) < 1000


def test_legacy_import_keeps_unique_startup_error_and_removes_raw_files(isolated):
    Path(store.MANAGED_LOG).write_text("server is listening\nque start_loop: waiting for new tasks\n", encoding="utf-8")
    Path(store.MANAGED_LOG + ".startup").write_text("server is listening\nunique loader error", encoding="utf-8")
    logs.import_legacy()
    selected = logs.latest()
    assert "unique loader error" in logs.console(selected)["content"]
    assert not Path(store.MANAGED_LOG).exists() and not Path(store.MANAGED_LOG + ".startup").exists()
    logs.import_legacy()
    assert len(logs.list_runs()["items"]) == 1


@pytest.mark.parametrize("boundary", [b"x", b"\n"])
def test_large_legacy_import_bounds_reads_and_keeps_startup_and_tail(isolated, monkeypatch, boundary):
    source = Path(store.MANAGED_LOG)
    size = 128 * 1024 * 1024
    tail = b"\nrecent loader error\n"
    with source.open("wb") as stream:
        stream.write(b"print_info: n_layer = 42\nserver is listening\nstartup warning\n")
        stream.seek(size - logs.LEGACY_WINDOW - 1)
        stream.write(boundary + b"partial tail error\n")
        stream.seek(size - len(tail))
        stream.write(tail)

    # Fail if migration scans the omitted middle, including an oversized line.
    original = Path.open
    read_bytes = 0

    class BoundedReader:
        def __enter__(self):
            self.stream = original(source, "rb")
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def seek(self, offset):
            return self.stream.seek(offset)

        def read(self, length):
            nonlocal read_bytes
            assert 0 <= length <= logs.LEGACY_WINDOW
            result = self.stream.read(length)
            read_bytes += len(result)
            assert read_bytes <= 2 * logs.LEGACY_WINDOW + 1
            return result

    def guarded_open(path, *args, **kwargs):
        if path == source and args == ("rb",):
            return BoundedReader()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    logs.import_legacy()
    selected = logs.latest()
    result = logs.console(selected)
    assert "startup warning" in result["content"] and "recent loader error" in result["content"]
    assert ("partial tail error" in result["content"]) == (boundary == b"\n")
    assert "omitted middle" in result["content"]
    record = logs.load(selected)
    assert record["gap"] and record["startup_truncated"]
    assert record["metadata"]["placement"]["blocks"][0]["total"] == 42
    assert record["stop_reason"] == "legacy_import"
    assert not source.exists()


def test_storage_failure_has_reserved_error_channel(isolated, monkeypatch):
    capture = writer()
    original = logs.atomic
    def fail(*args):
        raise OSError("disk is full")
    monkeypatch.setattr(logs, "atomic", fail)
    capture.publish()
    assert "disk is full" in logs.load(capture.run_id)["logging_error"]
    logs.stop_intent(capture.run_id, "manual_stop")
    assert "Cannot record stop intent" in logs.load(capture.run_id)["logging_error"]
    monkeypatch.setattr(logs, "atomic", original)
    capture.feed("server is listening")
    assert logs.console(capture.run_id)["error"]


@pytest.mark.parametrize("identifier", ["../state.json", "..", "/data", "", "A" * 32])
def test_run_identifiers_are_not_paths(isolated, identifier):
    with pytest.raises(ValueError):
        logs.console(identifier)


def test_run_listing_pagination_and_cursor_validation(isolated):
    first = writer()
    first.finish(2)
    second = writer()
    page = logs.list_runs(1)
    assert page["items"][0]["run_id"] == second.run_id
    assert logs.list_runs(1, page["next_cursor"])["items"][0]["exit_code"] == 2
    with pytest.raises(ValueError):
        logs.console(second.run_id, "not-a-cursor")


def test_real_capture_preserves_exit_and_trailing_output(isolated):
    selected = uuid.uuid4().hex
    handle = log_capture.launch([sys.executable, "-u", "-c",
        "import sys; print('server is listening'); sys.stderr.write('early loader error'); sys.exit(7)"],
        str(isolated), selected, "CPU test")
    assert handle.wait(timeout=10) == 7
    record = logs.load(selected)
    assert record["exit_code"] == 7 and record["stop_reason"] == "unexpected_exit"
    assert "early loader error" in logs.console(selected)["content"]


def test_capture_and_inference_survive_backend_exit(isolated):
    selected = uuid.uuid4().hex
    script = (
        "import log_capture,os,sys; "
        f"log_capture.launch([sys.executable,'-u','-c',"
        "\"import time; print('server is listening'); time.sleep(0.4); print('warning: survived backend exit')\"],"
        f"{str(isolated)!r},{selected!r},'survival'); os._exit(0)"
    )
    backend = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=15)
    assert backend.returncode == 0, backend.stderr.decode()
    deadline = time.monotonic() + 10
    while logs.load(selected).get("state") != "exited" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert logs.load(selected)["exit_code"] == 0
    assert "survived backend exit" in logs.console(selected)["content"]


def test_stop_intent_is_durable_and_actual_pid_is_signalled(isolated):
    selected = uuid.uuid4().hex
    handle = log_capture.launch([sys.executable, "-u", "-c", "import time; print('server is listening'); time.sleep(60)"],
                                str(isolated), selected, "CPU test")
    try:
        assert handle.pid != handle.supervisor.pid
        logs.stop_intent(selected, "gaming_suspension")
        handle.terminate()
        handle.wait(timeout=10)
        assert logs.load(selected)["stop_reason"] == "gaming_suspension"
    finally:
        if handle.poll() is None:
            handle.kill()
            handle.wait(timeout=10)


def test_gpu_identity_and_placement_snapshot_survive_restart(isolated):
    capture = writer()
    emit(capture, "using device CUDA0 (Test GPU) (0000:01:00.0) - available")
    emit(capture, "print_info: n_layer = 1")
    emit(capture, "load_tensors: layer 0 assigned to device CUDA0")
    emit(capture, "load_tensors: layer 1 assigned to device CPU")
    db = HistoryDB(str(isolated / "history.sqlite"))
    follower = LogFollower(db, store.MANAGED_LOG, run_id=capture.run_id, managed=True)
    follower.poll()
    follower = LogFollower(db, store.MANAGED_LOG, run_id=capture.run_id, managed=True)
    assert follower.placement.device_indices({"devices": [{"index": 7, "pci_bus_id": "00000000:01:00.0"}]}) == {"CUDA0": 7}
    assert follower.placement.snapshot()["output_device"] == "CPU"


def test_startup_and_unicode_records_are_bounded(isolated, monkeypatch):
    monkeypatch.setattr(logs, "STARTUP_LIMIT", 500)
    capture = writer()
    for _ in range(20):
        emit(capture, "unimportant startup trace " + "x" * 90)
    emit(capture, "load_tensors: layer 0 assigned to device CPU")
    emit(capture, "warning " + "\U0001f680" * 60000, "warning")
    result = logs.console(capture.run_id)
    assert "log record truncated" in result["content"]
    assert logs.load(capture.run_id)["startup_truncated"]
    assert all(len(line) <= logs.MAX_LINE for p in logs.segments(capture.run_id) for line in p.read_bytes().splitlines(keepends=True))


def test_capture_drains_output_when_segment_writes_fail(isolated):
    selected = uuid.uuid4().hex
    script = """
import log_capture, managed_logs
original = managed_logs.Path.open
def open_file(path, *args, **kwargs):
    if path.suffix == '.jsonl' and args and args[0] == 'ab':
        raise OSError('simulated disk full')
    return original(path, *args, **kwargs)
managed_logs.Path.open = open_file
log_capture.supervise(__import__('sys').argv[1])
"""
    payload = {"argv": [sys.executable, "-u", "-c", "for _ in range(2000): print('warning: ' + 'x'*2000)"],
               "managed_log": store.MANAGED_LOG, "cwd": str(isolated), "name": "storage test"}
    result = subprocess.run([sys.executable, "-c", script, selected], input=json.dumps(payload).encode(),
                            capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode()
    record = logs.load(selected)
    assert record["exit_code"] == 0 and "disk full" in record["logging_error"]


def test_unclean_supervisor_exit_preserves_ownership_of_live_server(isolated):
    selected = uuid.uuid4().hex
    handle = log_capture.launch([sys.executable, "-u", "-c", "import time; time.sleep(60)"],
                                str(isolated), selected, "capture failure")
    try:
        handle.supervisor.kill()
        handle.supervisor.wait(timeout=10)
        logs.reconcile()
        assert handle.poll() is None
        assert "still running" in logs.load(selected)["logging_error"]
        handle.kill()
        deadline = time.monotonic() + 10
        while handle.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        logs.reconcile()
        record = logs.load(selected)
        assert record["state"] == "interrupted" and record["exit_code"] is None
        assert record["ended_at"] is None
    finally:
        handle.kill()


def test_native_readoption_uses_actual_identity_and_keeps_exit_code(isolated, monkeypatch):
    import launcher
    model = isolated / "model.gguf"
    model.touch()
    store.update_settings(llama_server_path=sys.executable)
    first = launcher.ServerManager(lambda *args: None)
    monkeypatch.setattr(first, "build_argv", lambda *args: [sys.executable, "-u", "-c", "import time; time.sleep(60)"])
    first.launch({"model_path": str(model), "port": 8001})
    second = launcher.ServerManager(lambda *args: None)
    # The CPU fixture uses Python rather than an executable named llama-server.
    monkeypatch.setattr(launcher, "_process_alive", lambda pid: True)
    try:
        second.adopt()
        assert second.status()["adopted"]
        assert second.status()["pid"] == first.status()["pid"]
        second.stop("restart")
        assert second.exit_code is not None
        assert logs.load(second.run_id)["stop_reason"] == "restart"
    finally:
        if first._proc and first._proc.poll() is None:
            first.stop()
