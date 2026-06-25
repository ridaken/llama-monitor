"""Tests for launcher.py — argv construction, validation, and the launch /
stop / restart lifecycle. A fake Popen stands in for a real llama-server so the
tests never load a model or open a port.
"""

import sys

import pytest

import launcher
import store
from launcher import LaunchError, ServerManager, _flag_tokens, resolve_binary


# --------------------------------------------------------------------------- #
# argv                                                                         #
# --------------------------------------------------------------------------- #

def test_flag_tokens_bare_switch_vs_valued():
    flags = [
        {"flag": "-c", "value": "4096"},
        {"flag": "--mlock", "value": ""},     # bare switch
        {"flag": "", "value": "ignored"},     # empty flag dropped
        {"flag": "-fa", "value": "on"},
    ]
    assert _flag_tokens(flags) == ["-c", "4096", "--mlock", "-fa", "on"]


def test_build_argv_injects_managed_flags_last():
    mgr = ServerManager(lambda *a: None)
    cfg = {"model_path": "C:/m/model.gguf", "port": 8001,
           "flags": [{"flag": "-c", "value": "86000"}]}
    argv = mgr.build_argv(cfg, "llama-server")

    assert argv[:3] == ["llama-server", "-m", "C:/m/model.gguf"]
    assert "-c" in argv and "86000" in argv
    # llama-monitor owns these three and appends them last so they win.
    assert "--metrics" in argv
    assert argv[-2:] == ["--log-file", store.MANAGED_LOG]
    p = argv.index("--port")
    assert argv[p + 1] == "8001"


# --------------------------------------------------------------------------- #
# binary resolution                                                            #
# --------------------------------------------------------------------------- #

def test_resolve_binary_absolute_path(tmp_path):
    exe = tmp_path / "llama-server.exe"
    exe.write_text("", encoding="utf-8")
    assert resolve_binary(str(exe)) == str(exe)


def test_resolve_binary_absolute_missing_returns_none(tmp_path):
    assert resolve_binary(str(tmp_path / "nope.exe")) is None


def test_resolve_binary_bare_name_uses_path(monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which",
                        lambda name: "/usr/bin/llama-server" if name == "llama-server" else None)
    assert resolve_binary("llama-server") == "/usr/bin/llama-server"
    assert resolve_binary(None) == "/usr/bin/llama-server"


# --------------------------------------------------------------------------- #
# validation                                                                   #
# --------------------------------------------------------------------------- #

@pytest.fixture
def good_settings(monkeypatch, tmp_path):
    """A valid binary + a managed log under tmp, so only the config under test
    determines whether launch() raises."""
    monkeypatch.setattr(store, "HOME_DIR", str(tmp_path))
    monkeypatch.setattr(store, "MANAGED_LOG", str(tmp_path / "llama-server.log"))
    monkeypatch.setattr(store, "get_settings",
                        lambda: {"llama_server_path": sys.executable, "default_port": 8001})
    return tmp_path


def _gguf(tmp_path):
    f = tmp_path / "model.gguf"
    f.write_text("", encoding="utf-8")
    return str(f)


def test_launch_requires_a_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "get_settings",
                        lambda: {"llama_server_path": str(tmp_path / "missing-llama")})
    mgr = ServerManager(lambda *a: None)
    with pytest.raises(LaunchError, match="not found"):
        mgr.launch({"model_path": _gguf(tmp_path), "port": 8001})


def test_launch_rejects_missing_model(good_settings):
    mgr = ServerManager(lambda *a: None)
    with pytest.raises(LaunchError, match="No model file"):
        mgr.launch({"model_path": "", "port": 8001})
    with pytest.raises(LaunchError, match="not found"):
        mgr.launch({"model_path": str(good_settings / "ghost.gguf"), "port": 8001})


def test_launch_rejects_non_gguf(good_settings):
    bad = good_settings / "model.bin"
    bad.write_text("", encoding="utf-8")
    mgr = ServerManager(lambda *a: None)
    with pytest.raises(LaunchError, match=".gguf"):
        mgr.launch({"model_path": str(bad), "port": 8001})


def test_launch_rejects_bad_port(good_settings):
    mgr = ServerManager(lambda *a: None)
    with pytest.raises(LaunchError, match="between 1 and 65535"):
        mgr.launch({"model_path": _gguf(good_settings), "port": 70000})


# --------------------------------------------------------------------------- #
# lifecycle (fake process)                                                     #
# --------------------------------------------------------------------------- #

class FakeProc:
    def __init__(self, argv, **kw):
        self.argv = argv
        self.pid = 4321
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 1   # Windows-style non-zero on terminate

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


@pytest.fixture
def fake_popen(monkeypatch):
    captured = {}

    def _popen(argv, **kw):
        captured["argv"] = argv
        captured["proc"] = FakeProc(argv, **kw)
        return captured["proc"]

    monkeypatch.setattr(launcher.subprocess, "Popen", _popen)
    return captured


def test_launch_spawns_retargets_and_reports_running(good_settings, fake_popen):
    calls = []
    mgr = ServerManager(lambda *a: calls.append(a))
    status = mgr.launch({"name": "c1", "model_path": _gguf(good_settings), "port": 9001})

    assert status["state"] == "running"
    assert status["config_name"] == "c1"
    # Spawned with our argv, and the dashboard was retargeted at the new server.
    assert fake_popen["argv"][:2] == [sys.executable, "-m"]
    assert calls == [("http://127.0.0.1:9001", store.MANAGED_LOG, 9001)]


def test_explicit_stop_reports_stopped_not_exited(good_settings, fake_popen):
    """Regression: an explicit Stop must not look like a crash even though
    terminate() yields a non-zero return code."""
    mgr = ServerManager(lambda *a: None)
    mgr.launch({"name": "c1", "model_path": _gguf(good_settings), "port": 9001})
    status = mgr.stop()
    assert status["state"] == "stopped"


def test_crash_reports_exited_with_code(good_settings, fake_popen):
    mgr = ServerManager(lambda *a: None)
    mgr.launch({"name": "c1", "model_path": _gguf(good_settings), "port": 9001})
    # Simulate the process dying on its own (e.g. bad flag / OOM).
    fake_popen["proc"].returncode = 1
    status = mgr.status()
    assert status["state"] == "exited"
    assert status["exit_code"] == 1


def test_restart_requires_prior_launch():
    mgr = ServerManager(lambda *a: None)
    with pytest.raises(LaunchError, match="Nothing to restart"):
        mgr.restart()
