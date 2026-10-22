"""Container contracts without requiring Docker or an NVIDIA device."""
import argparse
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

import app as app_module
import launcher
import store


@pytest.fixture
def container(monkeypatch, tmp_path):
    monkeypatch.setenv("LLAMA_MONITOR_CONTAINER", "1")
    for name, value in {"HOME_DIR": tmp_path, "STATE_PATH": tmp_path / "state.json",
                        "MANAGED_LOG": tmp_path / "llama-server.log",
                        "PROMPTS_DIR": tmp_path / "prompts"}.items():
        monkeypatch.setattr(store, name, str(value))
    monkeypatch.setattr(app_module.GpuCollector, "__init__", lambda self: None)
    monkeypatch.setattr(app_module.GpuCollector, "shutdown", lambda self: None)
    args = argparse.Namespace(llama_url=app_module.DEFAULT_LLAMA_URL, llama_log=None,
                              llama_prompts_dir=None, port=8500, host="0.0.0.0")
    return args


def test_data_directory_is_configurable_without_changing_native_default(tmp_path):
    env = {**os.environ, "LLAMA_MONITOR_DATA_DIR": str(tmp_path)}
    command = [sys.executable, "-c", "import store; print(store.HOME_DIR)"]
    assert subprocess.check_output(command, env=env, text=True).strip() == str(tmp_path)
    env.pop("LLAMA_MONITOR_DATA_DIR")
    assert subprocess.check_output(command, env=env, text=True).strip() == os.path.join(
        os.path.expanduser("~"), ".llama-monitor")


def test_container_starts_without_model_and_clears_stale_pid(container, monkeypatch):
    store.set_running({"pid": 123, "config": {"model_path": "old.gguf"}})
    monkeypatch.setattr(launcher.ServerManager, "adopt", lambda self: pytest.fail("must not adopt"))
    monkeypatch.setattr(app_module.GamingCoordinator, "start", lambda self: pytest.fail("no Windows startup"))
    stopped = []
    monkeypatch.setattr(launcher.ServerManager, "stop", lambda self: stopped.append(True))
    with TestClient(app_module.build_app(container)) as client:
        assert client.get("/api/health").json() == {"status": "ok"}
        state = client.get("/api/launcher/state").json()
        assert state["status"]["state"] == "stopped"
        assert state["settings"]["models_dir"] == "/models"
        assert state["runtime"] == {"containerized": True, "models_dir": "/models", "managed_port": 8001}
        assert store.get_running() is None
    assert stopped == [True]


@pytest.mark.parametrize("path", ["/api/startup/install", "/api/startup/remove",
    "/api/gaming/settings", "/api/gaming/test", "/api/gaming/prepare",
    "/api/gaming/session-ended", "/api/gaming/auxiliary", "/api/gaming/retry",
    "/api/gaming/cancel", "/api/gaming/servers/primary/stop"])
def test_windows_controls_are_unavailable_even_over_loopback(container, path):
    with TestClient(app_module.build_app(container), client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/gaming/state").json()["supported"] is False
        assert client.get("/api/startup/state").json()["supported"] is False
        response = client.post(path, json={})
        assert response.status_code >= 400
        assert "native Windows" in response.json()["error"]


def test_container_launch_enforces_published_port_and_host(container, monkeypatch, tmp_path):
    binary = tmp_path / "llama-server"
    binary.touch()
    model = tmp_path / "model.gguf"
    model.touch()
    store.update_settings(llama_server_path=str(binary))
    manager = launcher.ServerManager(lambda *args: None)
    config = {"model_path": str(model), "port": 9000, "flags": []}
    with pytest.raises(launcher.LaunchError, match="internal port 8001"):
        manager.launch(config)
    config["port"] = 8001
    config["flags"] = [{"flag": "--host", "value": "127.0.0.1"}]
    monkeypatch.setattr(launcher.flags_mod, "get_server_flags", lambda _: {"source": "bundled", "flags": []})
    argv = manager.build_argv(config, str(binary))
    assert argv[argv.index("--host", argv.index("--host") + 1) + 1] == "0.0.0.0"
    assert argv[argv.index("--port") + 1] == "8001"
    monkeypatch.delenv("LLAMA_MONITOR_CONTAINER")
    native = manager.build_argv({**config, "flags": []}, str(binary))
    assert "--host" not in native
    assert store._default_state()["settings"]["models_dir"] is None


def test_container_telemetry_never_matches_nvml_host_pids(container, monkeypatch):
    seen = []
    monkeypatch.setattr(app_module, "find_llama_pids", lambda **kwargs: {42})
    monkeypatch.setattr(app_module.GpuCollector, "collect", lambda self, pids: seen.append(pids) or {"devices": []})
    monkeypatch.setattr(app_module.LlamaCollector, "collect", lambda *args: {"online": False})
    monkeypatch.setattr(app_module, "collect_sysmem", lambda pids: {"llama_rss": len(pids)})
    with TestClient(app_module.build_app(container)) as client:
        result = client.get("/api/stats").json()
        assert result["runtime"]["containerized"] is True
        assert result["sysmem"]["llama_rss"] == 1
    assert seen == [set()]
