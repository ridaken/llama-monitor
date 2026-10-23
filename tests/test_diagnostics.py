"""Diagnostics exercise subprocess failure modes without a GPU or model."""
import sys

import pytest

import diagnostics


def test_probe_preserves_stderr_and_nonzero_exit():
    report = diagnostics.probe("server_devices", [sys.executable, "-c",
        "import sys; print('loader error', file=sys.stderr); sys.exit(7)"])
    assert report["state"] == "fail" and report["exit_code"] == 7
    assert "loader error" in report["output"]


def test_probe_bounds_output():
    report = diagnostics.probe("large", [sys.executable, "-c", "print('x' * 65536)"])
    assert report["state"] == "pass" and report["truncated"]
    assert len(report["output"]) <= diagnostics.OUTPUT_LIMIT


def test_probe_timeout_is_reported(monkeypatch):
    monkeypatch.setattr(diagnostics, "PROBE_TIMEOUT", 0.5)
    report = diagnostics.probe("slow", [sys.executable, "-c",
        "import time; print('starting', flush=True); time.sleep(30)"])
    assert report["state"] == "fail" and "timed out" in report["message"]
    assert report["exit_code"] is None


def test_probe_missing_executable():
    assert diagnostics.probe("missing", ["missing-diagnostic-program-928123"])["state"] == "fail"


@pytest.mark.parametrize("nvidia_state", ["pass", "fail", "skipped"])
def test_doctor_reports_gpu_evidence_and_recovery(monkeypatch, nvidia_state):
    monkeypatch.setattr(diagnostics, "resolve_binary", lambda _: "server")
    monkeypatch.setattr(diagnostics.store, "get_settings", lambda: {})
    monkeypatch.setattr(diagnostics.shutil, "which", lambda _: None if nvidia_state == "skipped" else "smi")
    monkeypatch.setattr(diagnostics.runtime, "containerized", lambda: True)
    commands = []
    def probe(name, argv):
        commands.append(argv)
        return {"name": name, "state": nvidia_state if name == "nvidia" else "pass"}
    monkeypatch.setattr(diagnostics, "probe", probe)
    report = diagnostics.doctor()
    assert report["healthy"] == (nvidia_state != "fail")
    assert bool(report["recovery"]) == (nvidia_state == "fail")
    assert commands[0] == ["server", "--list-devices"]
    assert report["checks"][1]["state"] == nvidia_state
    if report["recovery"]:
        assert "all Docker containers" in " ".join(report["recovery"])


def test_server_segfault_does_not_claim_gpu_failure(monkeypatch):
    monkeypatch.setattr(diagnostics, "resolve_binary", lambda _: "server")
    monkeypatch.setattr(diagnostics.store, "get_settings", lambda: {})
    monkeypatch.setattr(diagnostics.shutil, "which", lambda _: "smi")
    monkeypatch.setattr(diagnostics.runtime, "containerized", lambda: True)
    monkeypatch.setattr(diagnostics, "probe", lambda name, argv: {
        "name": name, "state": "fail" if name == "server_devices" else "pass", "exit_code": -11})
    report = diagnostics.doctor()
    assert not report["healthy"] and not report["recovery"]


def test_missing_server_is_an_actionable_failure(monkeypatch):
    monkeypatch.setattr(diagnostics, "resolve_binary", lambda _: None)
    monkeypatch.setattr(diagnostics.store, "get_settings", lambda: {})
    monkeypatch.setattr(diagnostics.shutil, "which", lambda _: None)
    report = diagnostics.doctor()
    assert not report["healthy"] and "executable" in report["checks"][0]["message"]
