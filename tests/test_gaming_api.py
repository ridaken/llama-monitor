import argparse
import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import gaming
import store


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "HOME_DIR", str(tmp_path))
    monkeypatch.setattr(store, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(store, "MANAGED_LOG", str(tmp_path / "llama-server.log"))
    monkeypatch.setattr(app_module, "protect", lambda value: base64.b64encode(value.encode()).decode())
    monkeypatch.setattr(app_module, "unprotect", lambda value: base64.b64decode(value).decode())
    monkeypatch.setattr(app_module, "certificate_fingerprint", lambda url: "verified-local-certificate")
    monkeypatch.setattr(app_module.ApolloClient, "connected", lambda self, settings: [])
    monkeypatch.setattr(gaming.GamingCoordinator, "start", lambda self: None)
    args = argparse.Namespace(llama_url=app_module.DEFAULT_LLAMA_URL, llama_log=None, port=8500, host="127.0.0.1")
    with TestClient(app_module.build_app(args), client=("127.0.0.1", 50000)) as c:
        yield c


def configure(client):
    assert client.post("/api/gaming/settings", json={"username": "user", "password": "private-password"}).status_code == 200
    assert client.post("/api/gaming/test").status_code == 200
    assert client.post("/api/gaming/settings", json={"enabled": True}).status_code == 200


def test_defaults_and_secrets_are_not_returned(client):
    state = client.get("/api/gaming/state").json()
    assert state["enabled"] is False and state["phase"] == "normal"
    configure(client)
    response = client.get("/api/gaming/state")
    assert "private-password" not in response.text and "hook_token" not in response.text
    assert "credentials" not in client.get("/api/launcher/state").json()["settings"]
    assert response.json()["credentials_saved"] is True
    config = json.loads((Path(store.HOME_DIR) / "apollo-hook.json").read_text())
    assert config["port"] == 8500 and config["url"] == "http://127.0.0.1:8500"


def test_hook_authentication_and_switching(client):
    configure(client)
    assert client.post("/api/gaming/prepare").status_code == 409
    token = base64.b64decode(store.load_state()["gaming"]["hook_token"]).decode()
    headers = {"X-Llama-Monitor-Token": token}
    assert client.post("/api/gaming/prepare", headers=headers).json()["phase"] == "gaming"
    assert client.post("/api/gaming/session-ended", headers=headers).status_code == 202
    assert client.post("/api/launcher/launch", json={}).status_code == 400
    assert client.post("/api/gaming/settings", json={"enabled": False}).status_code == 400


def test_foreign_origin_and_nonlocal_controls_are_rejected(client):
    assert client.post("/api/gaming/settings", json={}, headers={"Origin": "https://evil.example"}).status_code == 400
    with TestClient(client.app, client=("192.168.1.22", 123)) as remote:
        assert remote.post("/api/gaming/settings", json={}).status_code == 400


def test_loopback_only_apollo_url_and_test_before_enable(client):
    assert client.post("/api/gaming/settings", json={"apollo_url": "https://example.com"}).status_code == 400
    assert client.post("/api/gaming/settings", json={"enabled": True}).status_code == 400


def test_disabled_hook_is_noop_and_does_not_block_streaming(client):
    client.post("/api/gaming/settings", json={"username": "user", "password": "pw"})
    token = base64.b64decode(store.load_state()["gaming"]["hook_token"]).decode()
    response = client.post("/api/gaming/prepare", headers={"X-Llama-Monitor-Token": token})
    assert response.status_code == 200 and response.json()["skipped"]
    assert store.load_state()["gaming"]["transition"] is None


def test_embedding_registration_does_not_expose_arguments(client, monkeypatch):
    monkeypatch.setattr(app_module, "resolve_binary", lambda path: "C:/llama-server.exe")
    monkeypatch.setattr(app_module, "discover_registered", lambda exe, port: {
        "executable": exe, "port": port, "argv": [exe, "--api-key", "secret"]})
    response = client.post("/api/gaming/auxiliary", json={"port": 8081})
    assert response.status_code == 200
    assert response.json()["auxiliary"]["port"] == 8081
    assert "secret" not in response.text and "argv" not in response.text
    assert client.post("/api/gaming/auxiliary", json={"port": 8001}).status_code == 400


def test_gaming_stats_clear_stale_memory_but_keep_hardware(client, monkeypatch):
    configure(client)
    token = base64.b64decode(store.load_state()["gaming"]["hook_token"]).decode()
    client.post("/api/gaming/prepare", headers={"X-Llama-Monitor-Token": token})
    monkeypatch.setattr(app_module.LlamaCollector, "collect", lambda self, *a: {
        "online": True, "model": {"name": "stale"}, "kv": {"tokens": 4000}})
    response = client.get("/api/stats").json()
    assert not response["online"] and response["split"] == []
    assert "model" not in response and "kv" not in response
    assert "gpu" in response and "sysmem" in response


def test_startup_controls_only_accept_local_same_origin_requests(client):
    assert client.get("/api/startup/state").json()["installed"] is False
    assert client.post("/api/startup/install", json={}, headers={"Origin": "https://evil.example"}).status_code == 400
    with TestClient(client.app, client=("192.168.1.22", 123)) as remote:
        assert remote.post("/api/startup/remove").status_code == 400


def test_startup_setup_uses_native_operation_not_password_body(client, monkeypatch):
    received = []
    def setup(self, **kwargs):
        received.append(kwargs)
        return {"pending": True}
    monkeypatch.setattr(app_module.WindowsStartup, "setup", setup)
    response = client.post("/api/startup/install", json={"mode": "boot", "autostart_models": True})
    assert response.status_code == 202
    assert received == [{"mode": "boot", "autostart_models": True}]
    assert "password" not in response.text
