"""Tests for launcher.py — argv construction, validation, and the launch /
stop / restart lifecycle. A fake Popen stands in for a real llama-server so the
tests never load a model or open a port.
"""

import os
import sys
import types

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


def test_flag_tokens_skips_disabled():
    """A per-flag toggle: enabled is False -> skipped; absent/True -> kept."""
    flags = [
        {"flag": "-c", "value": "4096", "enabled": True},   # explicit on
        {"flag": "-fa", "value": "on", "enabled": False},   # valued, off
        {"flag": "--mlock", "value": "", "enabled": False}, # bare switch, off
        {"flag": "-ngl", "value": "99"},                    # key absent -> on
    ]
    assert _flag_tokens(flags) == ["-c", "4096", "-ngl", "99"]


def test_build_argv_excludes_disabled_flags():
    mgr = ServerManager(lambda *a: None)
    cfg = {"model_path": "C:/m/model.gguf", "port": 8001,
           "flags": [{"flag": "-c", "value": "86000"},
                     {"flag": "--no-mmap", "value": "", "enabled": False}]}
    argv = mgr.build_argv(cfg, "llama-server")

    assert "-c" in argv and "86000" in argv
    assert "--no-mmap" not in argv                # disabled -> not launched
    # Managed flags still appended last so they win.
    assert "--metrics" in argv
    assert argv[-2:] == ["--log-file", store.MANAGED_LOG]


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


def test_build_argv_uses_only_advertised_jsonl_flags(monkeypatch):
    mgr = ServerManager(lambda *a: None)
    cfg = {"model_path": "model.gguf", "port": 8001, "flags": []}
    monkeypatch.setattr(launcher.flags_mod, "get_server_flags", lambda binary: {
        "source": "help", "flags": [{"flags": ["--log-jsonl"]}]})
    argv = mgr.build_argv(cfg, "llama-server")
    assert "--log-jsonl" in argv
    assert "--log-timestamps" not in argv
    monkeypatch.setattr(launcher.flags_mod, "get_server_flags", lambda binary: {
        "source": "bundled", "flags": [{"flags": ["--log-jsonl", "--log-timestamps"]}]})
    argv = mgr.build_argv(cfg, "llama-server")
    assert "--log-jsonl" not in argv and "--log-timestamps" not in argv


def test_prompt_logging_requires_native_flag(monkeypatch):
    mgr = ServerManager(lambda *a: None)
    cfg = {"model_path": "model.gguf", "port": 8001, "flags": [], "log_prompts": True}
    monkeypatch.setattr(launcher.flags_mod, "get_server_flags", lambda binary: {
        "source": "help", "flags": [{"flags": ["--log-prompts-dir"]}]})
    argv = mgr.build_argv(cfg, "llama-server")
    assert argv[argv.index("--log-prompts-dir") + 1] == store.PROMPTS_DIR
    monkeypatch.setattr(launcher.flags_mod, "get_server_flags", lambda binary: {
        "source": "bundled", "flags": [{"flags": ["--log-prompts-dir"]}]})
    with pytest.raises(LaunchError, match="does not support prompt logging"):
        mgr.build_argv(cfg, "llama-server")


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
    determines whether launch() raises. State is redirected to tmp so the
    persisted running-record never touches the real ~/.llama-monitor."""
    monkeypatch.setattr(store, "HOME_DIR", str(tmp_path))
    monkeypatch.setattr(store, "STATE_PATH", str(tmp_path / "state.json"))
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
        captured["kwargs"] = kw
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


def test_failed_stop_keeps_process_and_persisted_ownership(good_settings, fake_popen, monkeypatch):
    mgr = ServerManager(lambda *a: None)
    mgr.launch({"name": "c1", "model_path": _gguf(good_settings), "port": 9001})
    def denied():
        raise PermissionError("access denied")
    monkeypatch.setattr(fake_popen["proc"], "terminate", denied)
    with pytest.raises(LaunchError, match="Failed to stop"):
        mgr.stop()
    assert mgr.status()["state"] == "running"
    assert store.get_running()["pid"] == fake_popen["proc"].pid


def test_restore_reuses_exact_invocation_despite_settings_changes(good_settings, fake_popen, monkeypatch):
    def capture(pid):
        return {"pid": pid, "created_at": 1.0, "executable": sys.executable,
                "argv": list(fake_popen["argv"]), "cwd": str(good_settings)}
    monkeypatch.setattr(launcher, "capture_process", capture)
    monkeypatch.setattr(launcher, "identity_matches", lambda record: True)
    mgr = ServerManager(lambda *args: None)
    config = {"name": "unsaved", "model_path": _gguf(good_settings), "port": 9001,
              "flags": [{"flag": "-c", "value": "12345"}], "log_prompts": False}
    mgr.launch(config)
    snapshot = mgr.snapshot()
    mgr.stop()
    monkeypatch.setattr(store, "get_settings", lambda: {"llama_server_path": "does-not-exist"})
    restored = mgr.restore(snapshot)
    assert fake_popen["argv"] == snapshot["argv"]
    assert fake_popen["kwargs"]["cwd"] == snapshot["cwd"]
    assert restored["config"] == config


def test_restart_requires_prior_launch():
    mgr = ServerManager(lambda *a: None)
    with pytest.raises(LaunchError, match="Nothing to restart"):
        mgr.restart()


# --------------------------------------------------------------------------- #
# Survive a dashboard restart: detach on spawn + persist/re-adopt              #
# --------------------------------------------------------------------------- #

def test_launch_spawns_detached(good_settings, fake_popen):
    """The server must outlive the dashboard, so it's spawned detached from the
    dashboard's console / process group."""
    mgr = ServerManager(lambda *a: None)
    mgr.launch({"name": "c1", "model_path": _gguf(good_settings), "port": 9001})
    kw = fake_popen["kwargs"]
    if os.name == "nt":
        assert kw["creationflags"] & launcher.subprocess.CREATE_NEW_PROCESS_GROUP
        assert kw["creationflags"] & launcher.subprocess.DETACHED_PROCESS
    else:
        assert kw.get("start_new_session") is True


def test_launch_persists_running_record_and_stop_clears_it(good_settings, fake_popen):
    mgr = ServerManager(lambda *a: None)
    mgr.launch({"name": "c1", "model_path": _gguf(good_settings), "port": 9001})
    rec = store.get_running()
    assert rec is not None
    assert rec["port"] == 9001
    assert rec["pid"] == fake_popen["proc"].pid
    assert rec["log_path"] == store.MANAGED_LOG
    assert len(rec["run_id"]) == 32

    mgr.stop()
    assert store.get_running() is None


def test_adopt_reattaches_to_a_live_server(good_settings, monkeypatch):
    """A restarted dashboard re-adopts a server a previous run launched."""
    monkeypatch.setattr(launcher, "_process_alive", lambda pid: True)
    identity = {"pid": 4321, "created_at": 1.0, "executable": sys.executable,
                "argv": [sys.executable], "cwd": str(good_settings)}
    monkeypatch.setattr(launcher, "capture_process", lambda pid: identity)
    monkeypatch.setattr(launcher, "identity_matches", lambda record: True)
    store.set_running({"pid": 4321, "port": 9001, "config": {"name": "c1"},
                       "started_at": 1.0, "identity": identity, "log_path": str(good_settings / "x.log")})
    captured = []
    mgr = ServerManager(lambda *a: captured.append(a))

    rec = mgr.adopt()
    assert rec is not None and rec["port"] == 9001
    assert rec["run_id"] == store.get_running()["run_id"]
    st = mgr.status()
    assert st["state"] == "running"
    assert st["adopted"] is True
    assert st["pid"] == 4321
    assert st["config_name"] == "c1"


def test_adopt_clears_stale_record_when_process_gone(good_settings, monkeypatch):
    monkeypatch.setattr(launcher, "_process_alive", lambda pid: False)
    store.set_running({"pid": 4321, "port": 9001, "config": {"name": "c1"}})
    mgr = ServerManager(lambda *a: None)

    assert mgr.adopt() is None
    assert store.get_running() is None          # stale record dropped
    assert mgr.status()["state"] == "stopped"


def test_stop_adopted_server_terminates_pid_and_clears_record(good_settings, monkeypatch):
    monkeypatch.setattr(launcher, "_process_alive", lambda pid: True)
    seen = {}

    identity = {"pid": 4321, "created_at": 1.0, "executable": sys.executable,
                "argv": [sys.executable], "cwd": str(good_settings)}
    monkeypatch.setattr(launcher, "capture_process", lambda pid: identity)
    monkeypatch.setattr(launcher, "identity_matches", lambda record: True)
    def terminate(record, grace):
        seen.update(pid=record["pid"], terminated=True)
    monkeypatch.setattr(launcher, "terminate_verified", terminate)
    store.set_running({"pid": 4321, "port": 9001, "config": {"name": "c1"}, "identity": identity})
    mgr = ServerManager(lambda *a: None)
    mgr.adopt()

    st = mgr.stop()
    assert seen["pid"] == 4321 and seen.get("terminated") is True
    assert st["state"] == "stopped"
    assert store.get_running() is None
