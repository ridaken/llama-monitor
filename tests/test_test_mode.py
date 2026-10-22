import argparse
import os

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
