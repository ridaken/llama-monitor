import copy
from contextlib import nullcontext
from pathlib import Path
import subprocess
import sys

import psutil
import pytest

import processes


@pytest.fixture
def linux_process(monkeypatch, tmp_path):
    """A fake procfs and process; never inspect or signal a host PID."""
    monkeypatch.setattr(processes, "_LINUX", True)
    monkeypatch.setattr(processes.psutil, "PROCFS_PATH", str(tmp_path), raising=False)
    boot = tmp_path / "sys/kernel/random/boot_id"
    boot.parent.mkdir(parents=True)
    boot.write_text("test-boot\n")
    stat = tmp_path / "123/stat"
    stat.parent.mkdir()
    stat.write_text("123 (worker (name) here) S " + " ".join(["0"] * 18 + ["3709", "0"]))
    state = {"created_at": 1000.09, "executable": "/app/llama-server", "alive": True,
             "signals": [], "boot": boot, "stat": stat}

    class FakeProcess:
        def __init__(self, pid):
            assert pid == 123
            if not state["alive"]:
                raise psutil.NoSuchProcess(pid)
            self.pid = pid
        def oneshot(self): return nullcontext()
        def create_time(self): return state["created_at"]
        def exe(self): return state["executable"]
        def cmdline(self): return [state["executable"], "--port", "8001"]
        def cwd(self): return "/app"
        def status(self): return psutil.STATUS_RUNNING
        def poll(self): return None if state["alive"] else 0
        def terminate(self):
            state["signals"].append("terminate")
            state["alive"] = False
        def wait(self, timeout): return 0

    monkeypatch.setattr(processes.psutil, "Process", FakeProcess)
    return state


@pytest.mark.parametrize("offset", [-3600, -1, 1, 3600])
def test_linux_clock_changes_do_not_prevent_verified_stop(linux_process, offset):
    record = processes.capture_process(123)
    assert record["boot_id"] == "test-boot" and record["start_ticks"] == 3709
    linux_process["created_at"] += offset
    current = processes.capture_process(123)
    assert current["created_at"] != record["created_at"]
    assert processes.same_identity(record, current)
    assert processes.identity_matches(record)
    processes.terminate_verified(record)
    assert linux_process["signals"] == ["terminate"]


@pytest.mark.parametrize("changed", ["start_ticks", "boot_id", "executable"])
def test_changed_linux_identity_is_never_signalled(linux_process, changed):
    record = processes.capture_process(123)
    if changed == "start_ticks":
        stat = linux_process["stat"]
        stat.write_text(stat.read_text().replace("3709", "3710"))
    elif changed == "boot_id":
        linux_process["boot"].write_text("different-boot")
    else:
        linux_process["executable"] = "/other/llama-server"
    assert not processes.identity_matches(record)
    processes.terminate_verified(record)
    assert not linux_process["signals"]


def test_linux_legacy_record_retains_strict_timestamp_check(linux_process):
    record = processes.capture_process(123)
    del record["boot_id"], record["start_ticks"]
    assert processes.identity_matches(record)
    linux_process["created_at"] -= 1
    assert not processes.identity_matches(record)
    processes.terminate_verified(record)
    assert not linux_process["signals"]


@pytest.mark.parametrize("missing", ["boot_id", "start_ticks"])
def test_incomplete_stable_record_cannot_fall_back_to_timestamp(linux_process, missing):
    record = processes.capture_process(123)
    del record[missing]
    with pytest.raises(processes.ProcessError, match="incomplete"):
        processes.terminate_verified(record)
    assert not linux_process["signals"]


@pytest.mark.parametrize("failure", ["boot", "stat", "access"])
def test_unreadable_linux_identity_does_not_imply_exit(linux_process, monkeypatch, failure):
    record = processes.capture_process(123)
    if failure == "boot":
        linux_process["boot"].unlink()
    elif failure == "stat":
        linux_process["stat"].write_text("malformed")
    else:
        original = Path.read_text
        def denied(path, *args, **kwargs):
            if path == linux_process["stat"]:
                raise PermissionError("denied")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises((processes.ProcessError, psutil.AccessDenied)):
        processes.terminate_verified(record)
    assert not linux_process["signals"]


def test_capture_rejects_pid_reuse_during_inspection(linux_process, monkeypatch):
    identities = iter([{"boot_id": "test-boot", "start_ticks": 3709},
                       {"boot_id": "test-boot", "start_ticks": 3710}])
    monkeypatch.setattr(processes, "_linux_identity", lambda pid: next(identities))
    with pytest.raises(processes.ProcessError, match="changed during inspection"):
        processes.capture_process(123)


def test_snapshot_comparison_rejects_changed_pid(linux_process):
    record = processes.capture_process(123)
    assert not processes.same_identity(record, {**record, "pid": 124})


def test_auxiliary_snapshot_and_stop_survive_linux_clock_shift(linux_process):
    auxiliary = processes.AuxiliaryServer()
    auxiliary.record = processes.capture_process(123)
    auxiliary.proc = processes.psutil.Process(123)
    linux_process["created_at"] -= 1
    snapshot = auxiliary.snapshot({"executable": "/app/llama-server", "port": 8001})
    assert snapshot["start_ticks"] == 3709
    auxiliary.stop(snapshot)
    assert linux_process["signals"] == ["terminate"]
    assert auxiliary.proc is None and auxiliary.record is None


def test_capture_and_verified_stop_real_child():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        record = processes.capture_process(child.pid)
        assert record["pid"] == child.pid
        assert processes.identity_matches(record)
        processes.terminate_verified(record)
        child.wait(timeout=5)
        assert not processes.identity_matches(record)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_reused_pid_is_never_signalled(monkeypatch):
    signalled = []
    class FakeProcess:
        def __init__(self, pid): pass
        def create_time(self): return 20
        def exe(self): return "C:/llama-server.exe"
        def status(self): return psutil.STATUS_RUNNING
        def terminate(self): signalled.append(True)
    monkeypatch.setattr(processes.psutil, "Process", FakeProcess)
    original = {"pid": 100, "created_at": 10, "executable": "C:/llama-server.exe"}
    assert not processes.identity_matches(original)
    processes.terminate_verified(original)
    assert not signalled


def test_access_denied_is_not_mistaken_for_exit(monkeypatch):
    def denied(pid):
        raise psutil.AccessDenied(pid)
    monkeypatch.setattr(processes.psutil, "Process", denied)
    with pytest.raises(psutil.AccessDenied):
        processes.identity_matches({"pid": 1, "created_at": 1, "executable": "server"})


def test_registration_ignores_other_executables_and_ports(monkeypatch):
    class Candidate:
        def __init__(self, pid): self.pid, self.info = pid, {"name": "llama-server.exe"}
    records = {1: {"pid": 1, "executable": "C:/registered/llama-server.exe", "argv": ["llama-server", "--port", "8081"]},
               2: {"pid": 2, "executable": "C:/other/llama-server.exe", "argv": ["llama-server", "--port", "8081"]},
               3: {"pid": 3, "executable": "C:/registered/llama-server.exe", "argv": ["llama-server", "--port", "8001"]}}
    monkeypatch.setattr(processes.psutil, "process_iter", lambda attrs: [Candidate(pid) for pid in records])
    monkeypatch.setattr(processes, "capture_process", lambda pid: copy.deepcopy(records[pid]))
    result = processes.discover_registered("C:/registered/llama-server.exe", 8081)
    assert result["pid"] == 1


@pytest.mark.parametrize("argv", [["server", "--port", "8081"], ["server", "--port=8081"]])
def test_port_forms(argv):
    assert processes.argv_port(argv) == 8081


def test_last_port_flag_wins():
    assert processes.argv_port(["server", "--port", "8081", "--port", "8001"]) == 8001
