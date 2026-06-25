"""Integration tests for the FastAPI routes, driven through TestClient. These
cover the new launcher/config/browse surface end-to-end (minus actually
spawning a server). State is redirected to a temp file."""

import argparse

import pytest
from fastapi.testclient import TestClient

import app as app_module
import launcher
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


def test_default_config_endpoint_sets_and_clears(client):
    client.post("/api/configs", json={"name": "fav", "model_path": "C:/m.gguf",
                                       "port": 8001, "flags": []})
    body = client.post("/api/configs/default", json={"name": "fav"}).json()
    assert body["settings"]["default_config"] == "fav"

    body = client.post("/api/configs/default", json={"name": ""}).json()
    assert body["settings"]["default_config"] is None


def test_default_config_rejects_unknown(client):
    r = client.post("/api/configs/default", json={"name": "ghost"})
    assert r.status_code == 400
    assert "error" in r.json()


def test_deleting_default_config_clears_it(client):
    client.post("/api/configs", json={"name": "fav", "model_path": "C:/m.gguf",
                                      "port": 8001, "flags": []})
    client.post("/api/configs/default", json={"name": "fav"})
    client.delete("/api/configs/fav")
    state = client.get("/api/launcher/state").json()
    assert state["settings"]["default_config"] is None


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


def test_flags_endpoint_returns_bundled_without_binary(client):
    # The client fixture stubs shutil.which -> None, so no binary resolves.
    body = client.get("/api/launcher/flags").json()
    assert body["source"] == "bundled"
    assert isinstance(body["flags"], list) and body["flags"]
    assert all("flags" in f and "desc" in f for f in body["flags"])


def test_console_endpoint_tails_managed_log(client, tmp_path):
    # The dashboard starts with no log target; it falls back to MANAGED_LOG.
    import store
    log = store.MANAGED_LOG
    with open(log, "w", encoding="utf-8") as f:
        f.write("first line\n")

    first = client.get("/api/launcher/console", params={"offset": 0}).json()
    assert first["available"] is True
    assert "first line" in first["content"]

    # Append, then fetch only the new bytes from the returned offset.
    with open(log, "a", encoding="utf-8") as f:
        f.write("second line\n")
    nxt = client.get("/api/launcher/console", params={"offset": first["offset"]}).json()
    assert nxt["content"] == "second line\n"


def test_console_endpoint_missing_log_is_unavailable(client):
    body = client.get("/api/launcher/console", params={"offset": 0}).json()
    assert body["available"] is False
    assert body["content"] == ""


# --------------------------------------------------------------------------- #
# Explicit --llama-url takes precedence over re-adopting a launched server     #
# --------------------------------------------------------------------------- #

def _seed_running_server(monkeypatch, tmp_path):
    """Redirect state to tmp and record a (pretend-alive) launched server."""
    monkeypatch.setattr(store, "HOME_DIR", str(tmp_path))
    monkeypatch.setattr(store, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(store, "MANAGED_LOG", str(tmp_path / "llama-server.log"))
    monkeypatch.setattr(store.shutil, "which", lambda name: None)
    monkeypatch.setattr(launcher, "_process_alive", lambda pid: True)
    store.set_running({"pid": 4321, "port": 9001, "config": {"name": "c1"},
                       "started_at": 1.0, "log_path": str(tmp_path / "llama-server.log")})


def test_explicit_llama_url_skips_adoption(monkeypatch, tmp_path):
    _seed_running_server(monkeypatch, tmp_path)
    args = argparse.Namespace(llama_url="http://localhost:8001", llama_log=None,
                              port=8500, host="127.0.0.1")
    with TestClient(app_module.build_app(args)) as c:
        st = c.get("/api/launcher/state").json()["status"]
    # An explicit watch target wins -> the launched server is NOT adopted.
    assert st["state"] == "stopped"
    assert st["adopted"] is False


def test_default_llama_url_adopts_live_server(monkeypatch, tmp_path):
    _seed_running_server(monkeypatch, tmp_path)
    args = argparse.Namespace(llama_url=app_module.DEFAULT_LLAMA_URL, llama_log=None,
                              port=8500, host="127.0.0.1")
    with TestClient(app_module.build_app(args)) as c:
        st = c.get("/api/launcher/state").json()["status"]
    # No explicit target -> re-adopt the still-running launched server.
    assert st["state"] == "running"
    assert st["adopted"] is True
    assert st["config_name"] == "c1"
