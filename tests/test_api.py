"""Integration tests for the FastAPI routes, driven through TestClient. These
cover the new launcher/config/browse surface end-to-end (minus actually
spawning a server). State is redirected to a temp file."""

import argparse
import json

import pytest
from fastapi.testclient import TestClient

import app as app_module
import launcher
import store
from history import HistoryDB


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


def test_python_server_serves_built_react_ui(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "<div id=\"root\"></div>" in response.text
    assert "/static/app/assets/" in response.text
    assert response.headers["cache-control"] == "no-store"
    asset = response.text.split('/static/app/assets/')[1].split('"')[0]
    assert client.get('/static/app/assets/' + asset).status_code == 200


def test_history_api_filters_and_clear(client, tmp_path):
    db = HistoryDB(str(tmp_path / "history.sqlite"))
    db.ensure_run("api-run", "external", 1)
    db.save_batch(str(tmp_path / "server.log"), "key", 100, "api-run", [{
        "id": "g1", "run_id": "api-run", "model": "test-model", "slot_id": 2,
        "observed_at": 10, "completed_at": 11, "state": "complete",
        "prompt_tokens": 12, "generated_tokens": 5, "total_seconds": 1.2,
    }])
    body = client.get("/api/history", params={"model": "test-model", "state": "complete"}).json()
    assert body["items"][0]["id"] == "g1"
    assert body["models"] == ["test-model"]
    assert body["database_path"] == str(tmp_path / "history.sqlite")
    assert client.get("/api/history", params={"sort": "invalid"}).status_code == 400
    assert client.delete("/api/history").json()["deleted"] == 1
    assert client.get("/api/history").json()["items"] == []
    assert db.get_cursor(str(tmp_path / "server.log"))["offset"] == 100


def test_history_prompt_detail_and_prompt_only_filter(client, tmp_path):
    db = HistoryDB(str(tmp_path / "history.sqlite"))
    db.ensure_run("prompt-api-run", "external", 1)
    db.add_prompt_file("prompt-api-run", str(tmp_path / "000000000001.txt"),
                       10, "private prompt text", False)
    row = client.get("/api/history", params={"state": "prompt_only"}).json()["items"][0]
    assert row["has_prompt"] == 1
    assert client.get(f"/api/history/{row['id']}/prompt").json()["prompt_text"] == "private prompt text"
    client.delete("/api/history")
    assert client.get(f"/api/history/{row['id']}/prompt").status_code == 404


def test_console_decodes_jsonl_and_resumes_at_byte_offset(client):
    with open(store.MANAGED_LOG, "wb") as f:
        f.write((json.dumps({"type": "log", "msg": "server ready"}) + "\n").encode())
    first = client.get("/api/launcher/console").json()
    assert first["content"] == "server ready\n"
    with open(store.MANAGED_LOG, "ab") as f:
        f.write((json.dumps({"type": "log", "msg": "request done"}) + "\n").encode())
    second = client.get("/api/launcher/console", params={"offset": first["offset"]}).json()
    assert second["content"] == "request done\n"


def test_no_log_uses_http_idle_fallback(client, monkeypatch):
    levels = []
    def fake_collect(self, level="full"):
        levels.append(level)
        return {"online": False, "slots": {"busy": 0, "total": 0, "list": []},
                "requests": {"processing": 0, "deferred": 0}}
    monkeypatch.setattr(app_module.LlamaCollector, "collect", fake_collect)
    body = client.get("/api/stats", params={"lite": 1}).json()
    assert levels == ["slots"]
    assert body["log_mode"] is False
    assert body["log_status"]["configured"] is False


def test_config_round_trip(client):
    cfg = {"name": "api-cfg", "model_path": "C:/m.gguf", "port": 8001,
           "flags": [{"flag": "-c", "value": "4096"}]}
    assert client.post("/api/configs", json=cfg).status_code == 200

    listed = client.get("/api/configs").json()["configs"]
    assert [c["name"] for c in listed] == ["api-cfg"]

    after = client.delete("/api/configs/api-cfg").json()["configs"]
    assert after == []


def test_config_round_trip_preserves_disabled_flag(client):
    cfg = {"name": "toggled", "model_path": "C:/m.gguf", "port": 8001,
           "flags": [{"flag": "-c", "value": "4096"},
                     {"flag": "-fa", "value": "on", "enabled": False}]}
    assert client.post("/api/configs", json=cfg).status_code == 200

    listed = client.get("/api/configs").json()["configs"]
    saved = next(c for c in listed if c["name"] == "toggled")
    assert saved["flags"][1]["enabled"] is False


@pytest.mark.parametrize("enabled", [True, False])
def test_config_round_trip_preserves_prompt_logging(client, enabled):
    cfg = {"name": "prompts", "model_path": "C:/m.gguf", "port": 8001,
           "flags": [], "log_prompts": enabled}
    assert client.post("/api/configs", json=cfg).status_code == 200
    saved = client.get("/api/launcher/state").json()["configs"][0]
    assert saved["log_prompts"] is enabled
    with open(store.STATE_PATH, encoding="utf-8") as f:
        assert json.load(f)["configs"][0]["log_prompts"] is enabled


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


def test_adopted_server_exposes_actual_prompt_logging_config(monkeypatch, tmp_path):
    _seed_running_server(monkeypatch, tmp_path)
    saved = {"name": "c1", "model_path": "C:/m.gguf", "port": 9001,
             "flags": [], "log_prompts": False}
    store.upsert_config(saved)
    running = store.get_running()
    running["config"] = {**saved, "log_prompts": True}
    store.set_running(running)
    args = argparse.Namespace(llama_url=app_module.DEFAULT_LLAMA_URL, llama_log=None,
                              port=8500, host="127.0.0.1")
    with TestClient(app_module.build_app(args)) as c:
        body = c.get("/api/launcher/state").json()
    assert body["status"]["config"] == running["config"]
    assert body["configs"][0]["log_prompts"] is False
