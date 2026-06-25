"""Integration tests for the FastAPI routes, driven through TestClient. These
cover the new launcher/config/browse surface end-to-end (minus actually
spawning a server). State is redirected to a temp file."""

import argparse

import pytest
from fastapi.testclient import TestClient

import app as app_module
import store


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "HOME_DIR", str(tmp_path))
    monkeypatch.setattr(store, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(store, "MANAGED_LOG", str(tmp_path / "llama-server.log"))
    monkeypatch.setattr(store.shutil, "which", lambda name: None)

    args = argparse.Namespace(
        llama_url="http://127.0.0.1:9", llama_log=None, port=8500, host="127.0.0.1"
    )
    with TestClient(app_module.build_app(args)) as c:
        yield c


def test_launcher_state_shape(client):
    r = client.get("/api/launcher/state")
    assert r.status_code == 200
    body = r.json()
    assert set(["settings", "binary_valid", "configs", "status", "managed_log"]) <= body.keys()
    assert body["status"]["state"] == "stopped"


def test_config_round_trip(client):
    cfg = {"name": "api-cfg", "model_path": "C:/m.gguf", "port": 8001,
           "flags": [{"flag": "-c", "value": "4096"}]}
    assert client.post("/api/configs", json=cfg).status_code == 200

    listed = client.get("/api/configs").json()["configs"]
    assert [c["name"] for c in listed] == ["api-cfg"]

    after = client.delete("/api/configs/api-cfg").json()["configs"]
    assert after == []


def test_config_save_requires_name(client):
    r = client.post("/api/configs", json={"name": "", "model_path": "C:/m.gguf"})
    assert r.status_code == 400
    assert "error" in r.json()


def test_browse_lists_dirs_and_filters_files(client, tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "model.gguf").write_text("", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("", encoding="utf-8")

    body = client.get("/api/browse", params={"path": str(tmp_path), "ext": ".gguf"}).json()
    names = lambda paths: {p.replace("\\", "/").rstrip("/").split("/")[-1] for p in paths}
    assert "sub" in names(body["dirs"])
    assert names(body["files"]) == {"model.gguf"}   # .txt filtered out
    assert body["parent"] is not None


def test_launch_with_bad_model_returns_400(client):
    r = client.post("/api/launcher/launch",
                    json={"model_path": "C:/definitely/missing.gguf", "port": 8001, "flags": []})
    assert r.status_code == 400
    assert "error" in r.json()
