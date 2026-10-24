"""Default-model startup uses isolated state and never spawns a real model."""
import argparse
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import app as app_module
import gaming
import launcher
import store


@pytest.fixture
def startup(monkeypatch, tmp_path):
    monkeypatch.delenv("LLAMA_MONITOR_CONTAINER", raising=False)
    for key, path in {"HOME_DIR": tmp_path, "STATE_PATH": tmp_path / "state.json",
                      "MANAGED_LOG": tmp_path / "server.log", "PROMPTS_DIR": tmp_path / "prompts"}.items():
        monkeypatch.setattr(store, key, str(path))
    monkeypatch.setattr(app_module.GpuCollector, "__init__", lambda self: None)
    monkeypatch.setattr(app_module.GpuCollector, "shutdown", lambda self: None)
    monkeypatch.setattr(launcher.ServerManager, "adopt", lambda self: None)
    # Exercise real startup scheduling, but not the unrelated background loop.
    monkeypatch.setattr(gaming.GamingCoordinator, "_run", lambda self: None)
    config = {"name": "default", "model_path": str(tmp_path / "model.gguf"), "port": 8001,
              "log_prompts": True, "flags": [{"flag": "-c", "value": "4096"}]}
    store.upsert_config(config)
    store.set_default_config(config["name"])
    store.update_settings(autostart_default_model=True)
    args = argparse.Namespace(llama_url=app_module.DEFAULT_LLAMA_URL, llama_log=None, port=8500)
    return args, config


@pytest.mark.parametrize("containerized", [False, True])
def test_backend_startup_launches_once_without_browser(startup, monkeypatch, containerized):
    args, config = startup
    if containerized:
        monkeypatch.setenv("LLAMA_MONITOR_CONTAINER", "1")
        store.set_running({"pid": 123, "config": config})  # stale container PID
    launches = Mock(return_value={})
    monkeypatch.setattr(launcher.ServerManager, "launch", launches)
    with TestClient(app_module.build_app(args)) as client:
        launches.assert_called_once_with(config)
        for _ in range(3):
            assert client.get("/api/launcher/state").json()["autostart_error"] is None
        client.post("/api/launcher/stop")
        client.get("/api/launcher/state")
        launches.assert_called_once()


@pytest.mark.parametrize("reason", ["disabled", "no_default", "deleted", "external", "adopted", "unadopted"])
def test_startup_skips_other_priorities(startup, monkeypatch, reason):
    args, config = startup
    if reason == "disabled":
        store.update_settings(autostart_default_model=False)
    elif reason == "no_default":
        store.set_default_config(None)
    elif reason == "deleted":
        store.delete_config(config["name"])
    elif reason == "external":
        args.llama_url = "http://external.example:8001"
    elif reason == "adopted":
        monkeypatch.setattr(launcher.ServerManager, "adopt", lambda self: {"port": 8001})
        monkeypatch.setattr(launcher.ServerManager, "snapshot", lambda self: {"pid": 123})
    elif reason == "unadopted":
        store.set_running({"pid": 123, "config": config})
    launches = Mock()
    monkeypatch.setattr(launcher.ServerManager, "launch", launches)
    with TestClient(app_module.build_app(args)) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/launcher/state").json()["autostart_error"] is None
    launches.assert_not_called()


@pytest.mark.parametrize("clients", [[], ["Moonlight"], None])
def test_initial_gaming_connection_check_is_final(startup, clients):
    store.update_gaming(enabled=True)
    manager = Mock()
    manager.snapshot.return_value = None
    apollo = Mock()
    apollo.connected.return_value = clients
    if clients is None:
        apollo.connected.side_effect = OSError("Apollo unavailable")
    coordinator = gaming.GamingCoordinator(manager, apollo=apollo)
    coordinator.start_default_model()
    assert manager.launch.call_count == (1 if clients == [] else 0)
    # Neither a later disconnect, Stop, nor an exited model retries startup.
    apollo.connected.side_effect = None
    apollo.connected.return_value = []
    coordinator.stop_server()
    coordinator.start_default_model()
    assert manager.launch.call_count == (1 if clients == [] else 0)


@pytest.mark.parametrize("reason", ["gaming", "startup"])
def test_pending_recovery_wins_even_if_primary_is_stopped(startup, reason):
    store.update_gaming(transition={"reason": reason, "phase": "countdown", "servers": []})
    manager = Mock()
    coordinator = gaming.GamingCoordinator(manager)
    coordinator.start_default_model()
    manager.launch.assert_not_called()


def test_windows_boot_recovery_takes_priority_over_default(startup):
    manager = Mock()
    manager.snapshot.return_value = None
    coordinator = gaming.GamingCoordinator(manager)
    captured = {"id": "primary", "name": "captured model", "port": 8001,
                "config": {"name": "unsaved recovery", "model_path": "captured.gguf"}}
    # A completed Windows setup can enable recovery during backend startup.
    coordinator.startup_results = lambda: store.update_startup(
        installed=True, autostart_models=True, models=[captured])
    coordinator.start(autostart_default=True)
    coordinator.close()
    manager.launch.assert_not_called()
    assert coordinator.transition["reason"] == "startup"
    assert coordinator.primary_config() == captured["config"]


@pytest.mark.parametrize("failure", ["binary", "model", "spawn"])
def test_startup_errors_leave_dashboard_usable_without_retries(startup, monkeypatch, tmp_path, failure):
    args, config = startup
    monkeypatch.setattr(launcher, "resolve_binary", lambda path: None if failure == "binary" else "llama-server")
    if failure == "spawn":
        (tmp_path / "model.gguf").touch()
        monkeypatch.setattr(launcher.flags_mod, "get_server_flags", lambda binary: {
            "source": "help", "flags": [{"flags": ["--log-prompts-dir"]}]})
        monkeypatch.setattr(launcher.subprocess, "Popen", Mock(side_effect=OSError("spawn denied")))
    with TestClient(app_module.build_app(args)) as client:
        assert client.get("/api/health").json() == {"status": "ok"}
        error = client.get("/api/launcher/state").json()["autostart_error"]
        assert "Could not start the default model" in error
        assert {"binary": "executable not found", "model": "Model file not found", "spawn": "spawn denied"}[failure] in error
        for _ in range(3):
            assert client.get("/api/launcher/state").json()["autostart_error"] == error


def test_failed_startup_is_not_retried(startup):
    manager = Mock()
    manager.snapshot.return_value = None
    manager.launch.side_effect = launcher.LaunchError("invalid model")
    coordinator = gaming.GamingCoordinator(manager)
    coordinator.start_default_model()
    coordinator.start_default_model()
    manager.launch.assert_called_once()
    assert "invalid model" in coordinator.autostart_error
