import argparse
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import store


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    for key, value in {"HOME_DIR": tmp_path, "STATE_PATH": tmp_path / "state.json",
                       "MANAGED_LOG": tmp_path / "server.log", "PROMPTS_DIR": tmp_path / "prompts"}.items():
        monkeypatch.setattr(store, key, str(value))
    return argparse.Namespace(llama_url="http://127.0.0.1:9", llama_log=None, port=8765, test_mode=True)


def test_default_data_directory_is_rejected(isolated, monkeypatch):
    monkeypatch.setattr(store, "HOME_DIR", os.path.join(os.path.expanduser("~"), ".llama-monitor"))
    with pytest.raises(ValueError, match="separate"):
        app_module.build_app(isolated)


def test_seeded_automation_is_inert_and_test_owned_models_stop(isolated, monkeypatch):
    store.upsert_config({"name": "default", "model_path": "model.gguf", "port": 8001})
    store.set_default_config("default")
    store.update_settings(autostart_default_model=True)
    monkeypatch.setattr(app_module.ServerManager, "launch", lambda *args: pytest.fail("must not autostart"))
    store.set_running({"pid": 123, "config": {}})
    store.update_gaming(transition={"phase": "restoring", "servers": []}, enabled=True)
    store.update_startup(installed=True, pending={"operation": "never-consume"})
    monkeypatch.setattr(app_module.ServerManager, "adopt", lambda _: pytest.fail("must not adopt"))
    monkeypatch.setattr(app_module.GamingCoordinator, "start", lambda _: pytest.fail("must not automate"))
    stopped = []
    monkeypatch.setattr(app_module.ServerManager, "stop", lambda _: stopped.append(True))
    with TestClient(app_module.build_app(isolated), client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/launcher/state").json()["test_mode"]
        assert client.get("/api/gaming/state").json()["phase"] == "normal"
        assert client.get("/api/startup/state").json()["supported"] is False
        assert store.load_state()["startup"]["pending"] == {"operation": "never-consume"}
        assert store.get_running() is None
        for path in ("/api/startup/install", "/api/startup/remove", "/api/gaming/settings",
                     "/api/gaming/prepare", "/api/gaming/retry", "/api/gaming/cancel"):
            response = client.post(path, json={})
            assert response.status_code >= 400 and "test mode" in response.json()["error"]
    assert stopped == [True]


def test_main_ignores_inherited_targets_in_test_mode(isolated, monkeypatch):
    import sys
    from contextlib import nullcontext
    monkeypatch.setenv("LLAMA_URL", "http://external.example:8001")
    monkeypatch.setenv("LLAMA_LOG", "external.log")
    monkeypatch.setenv("LLAMA_PROMPTS_DIR", "external-prompts")
    monkeypatch.setattr(sys, "argv", ["app.py", "--test-mode", "--port", "8765"])
    monkeypatch.setattr(app_module, "backend_lock", lambda _: nullcontext())
    captured = []
    monkeypatch.setattr(app_module, "build_app", lambda args: captured.append(args))
    monkeypatch.setattr(app_module.uvicorn, "run", lambda *a, **kw: None)
    app_module.main()
    assert captured[0].llama_url == "http://127.0.0.1:9"
    assert captured[0].llama_log is None and captured[0].llama_prompts_dir is None


def test_main_rejects_default_directory_before_locking(isolated, monkeypatch):
    import sys
    monkeypatch.setattr(store, "HOME_DIR", os.path.join(os.path.expanduser("~"), ".llama-monitor"))
    monkeypatch.setattr(sys, "argv", ["app.py", "--test-mode"])
    monkeypatch.setattr(app_module, "backend_lock", lambda _: pytest.fail("must not write to normal state"))
    with pytest.raises(SystemExit) as error:
        app_module.main()
    assert error.value.code == 2


@pytest.mark.parametrize("flags,name", [
    ([], "weights.gguf"),
    ([{"flag": "--alias", "value": "Server alias"}], "Server alias"),
    ([{"flag": "-a", "value": "Short alias"}], "Short alias"),
    ([{"flag": "--alias", "value": "Disabled", "enabled": False}], "weights.gguf"),
    ([{"flag": "--alias", "value": "First"}, {"flag": "-a", "value": "Last"}], "Last"),
    ([{"flag": "--alias=Inline"}], "Inline"),
])
def test_launch_and_restart_history_have_model_without_stats(isolated, monkeypatch, flags, name):
    followers = []
    make_follower = app_module.LogFollower

    def capture_follower(*args, **kwargs):
        follower = make_follower(*args, **kwargs)
        followers.append(follower)
        return follower

    monkeypatch.setattr(app_module, "LogFollower", capture_follower)
    monkeypatch.setattr(make_follower, "start", lambda _: None)
    launches = []

    def fake_launch(manager, config):
        run_id = f"run-{len(launches)}"
        launches.append(run_id)
        manager.current = config
        Path(store.MANAGED_LOG).write_text("", encoding="utf-8")
        store.set_running({"pid": 123, "run_id": run_id, "config": config, "started_at": time.time()})
        manager._retarget("http://127.0.0.1:9", store.MANAGED_LOG, 9)
        Path(store.MANAGED_LOG).write_text(
            "slot launch_slot: id 0 | processing task 1\n"
            "slot print_timing: id 0 | task 1 | prompt eval time = 100 ms / 12 tokens\n"
            "slot print_timing: id 0 | task 1 | eval time = 200 ms / 7 tokens\n"
            "slot print_timing: id 0 | task 1 | total time = 300 ms / 19 tokens\n", encoding="utf-8")
        return manager.status()

    monkeypatch.setattr(app_module.ServerManager, "launch", fake_launch)
    config = {"name": "Saved profile", "model_path": "/models/weights.gguf", "port": 9001, "flags": flags}
    with TestClient(app_module.build_app(isolated)) as client:
        for action in ("launch", "restart"):
            assert client.post(f"/api/launcher/{action}", json=config).status_code == 200
            followers[0].poll()
            page = client.get("/api/history", params={"model": name}).json()
            assert any(row["run_id"] == launches[-1] and row["state"] == "complete" for row in page["items"]), page
        assert len(launches) == 2
