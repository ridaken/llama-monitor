import copy
import subprocess
import sys

import psutil
import pytest

import processes


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
