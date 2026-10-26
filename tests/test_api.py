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
    assert body["autostart_error"] is None


def test_autostart_setting_round_trip_without_immediate_launch(client, monkeypatch):
    monkeypatch.setattr(launcher.ServerManager, "launch", lambda *args: pytest.fail("must wait for restart"))
    for enabled in (True, False):
        response = client.post("/api/launcher/settings", json={"autostart_default_model": enabled})
        assert response.status_code == 200
        assert response.json()["settings"]["autostart_default_model"] is enabled
        assert client.get("/api/launcher/state").json()["settings"]["autostart_default_model"] is enabled
    # Partial settings updates must preserve the preference.
    client.post("/api/launcher/settings", json={"autostart_default_model": True})
    assert client.post("/api/launcher/settings", json={"models_dir": "models"}).json()["settings"]["autostart_default_model"] is True


@pytest.mark.parametrize("value", [None, 0, 1, "true", "false", [], {}])
def test_autostart_setting_rejects_non_booleans_atomically(client, value):
    response = client.post("/api/launcher/settings", json={"autostart_default_model": value, "default_port": 9000})
    assert response.status_code == 400
    assert "boolean" in response.json()["error"]
    assert store.get_settings()["autostart_default_model"] is False
    assert store.get_settings()["default_port"] == 8001


@pytest.mark.parametrize("process_bytes", [None, 1024])
def test_memory_stats_map_log_identity_and_nvml_ordinal(monkeypatch, tmp_path, process_bytes):
    monkeypatch.setattr(store, "HOME_DIR", str(tmp_path))
    monkeypatch.setattr(store, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(store, "MANAGED_LOG", str(tmp_path / "managed.log"))
    monkeypatch.setattr(store.shutil, "which", lambda name: None)
    monkeypatch.setattr(app_module.runtime, "containerized", lambda: False)
    monkeypatch.setattr(app_module.GpuCollector, "collect", lambda *args: {"ok": True, "devices": [
        {"index": 7, "pci_bus_id": "00000000:01:00.0", "llama_mem": process_bytes}]})
    monkeypatch.setattr(app_module.LlamaCollector, "collect", lambda *args: {"online": True})
    log = tmp_path / "server.log"
    log.write_text("llama_model_load: using device CUDA0 (GPU A) (0000:01:00.0) - 1024 MiB free\n"
                   "load_tensors: CUDA0 model buffer size = 100.00 MiB\n"
                   "load_tensors: CUDA1 model buffer size = 50.00 MiB\n", encoding="utf-8")
    args = argparse.Namespace(llama_url="http://127.0.0.1:9", llama_log=str(log), port=8500, host="127.0.0.1")
    with TestClient(app_module.build_app(args)) as client:
        result = client.get("/api/stats").json()
    if process_bytes is not None:
        assert result["split_source"] == "nvml"
        assert result["split"] == [{"label": "CUDA7", "bytes": 1024, "kind": "gpu", "gpu_index": 7}]
    else:
        assert result["split_source"] == "log"
        assert result["split"] == [
            {"label": "CUDA0", "bytes": 100 * 1024**2, "kind": "gpu", "gpu_index": 7},
            {"label": "CUDA1", "bytes": 50 * 1024**2, "kind": "gpu"}]


def test_memory_delta_requires_identity_and_honors_reordered_devices():
    baseline = {"CUDA0": 900, "CUDA1": 500}
    gpu = {"devices": [{"index": 7, "mem_total": 1000 * app_module.MIB, "mem_used": 300 * app_module.MIB},
                       {"index": 0, "mem_total": 2000 * app_module.MIB, "mem_used": 1500 * app_module.MIB}]}
    assert app_module._delta_split(baseline, gpu, {}) == []
    assert app_module._delta_split(baseline, gpu, {"CUDA0": 7}) == [
        {"label": "CUDA0", "bytes": 200 * app_module.MIB, "kind": "gpu", "gpu_index": 7}]


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


def test_activity_api_coverage_fallback_and_validation(client, tmp_path):
    db = HistoryDB(str(tmp_path / "history.sqlite"))
    db.ensure_run("activity-run", "external", 1)
    db.add_prompt_file("activity-run", str(tmp_path / "prompt.txt"), 10, "private native prompt", False)
    proxy = client.app.state.proxy
    metadata = {"id": "proxy1", "observed_at": 11, "endpoint": "/completion", "model": "Proxy model",
                "state": "complete", "usage": {"prompt_tokens": 0, "completion_tokens": 3}}
    proxy.history.begin(metadata)
    proxy.history.finish(metadata, "private request", "private response")
    result = client.get("/api/activity").json()
    assert [row["source"] for row in result["items"]] == ["proxy", "monitored"]
    assert result["items"][0]["prompt_tokens"] == 0
    assert result["items"][0]["generated_tokens"] == 3
    assert "private" not in json.dumps(result)
    assert "log_status" in result and "capture_error" in result and "database_path" in result
    assert client.get("/api/activity", params={"limit": 0}).status_code == 400
    assert client.get("/api/activity", params={"source": "unknown"}).status_code == 400
    assert client.get("/api/activity", params={"limit": "text"}).status_code == 422
    assert len(client.get("/api/history").json()["items"]) == 1
    assert len(client.get("/api/requests").json()["items"]) == 1
    proxy.history = None
    proxy.capture_error = "Storage unavailable"
    result = client.get("/api/activity").json()
    assert len(result["items"]) == 1 and result["items"][0]["source"] == "monitored"
    assert result["capture_error"] == "Storage unavailable"
    assert client.get("/api/activity", params={"source": "proxy"}).status_code == 503


def test_console_decodes_jsonl_and_resumes_at_byte_offset(client):
    with open(store.MANAGED_LOG, "wb") as f:
        f.write((json.dumps({"type": "log", "msg": "server ready"}) + "\n").encode())
    first = client.get("/api/launcher/console").json()
    assert first["content"] == "server ready\n"
    with open(store.MANAGED_LOG, "ab") as f:
        f.write((json.dumps({"type": "log", "msg": "request done"}) + "\n").encode())
    second = client.get("/api/launcher/console", params={"offset": first["offset"]}).json()
    assert second["content"] == "request done\n"


@pytest.mark.parametrize("text", ["CUDA initialization failed", "startup\nCUDA initialization failed"])
def test_console_falls_back_to_early_output_and_switches_source(client, text):
    with open(launcher.startup_log_path(), "w", encoding="utf-8") as output:
        output.write(text)
    first = client.get("/api/launcher/console").json()
    assert first["content"] == text
    assert first["source"] == "startup"
    # Offset belongs to a different file; don't skip the new structured log.
    with open(store.MANAGED_LOG, "w", encoding="utf-8") as output:
        output.write("managed model log with a longer first line\n")
    second = client.get("/api/launcher/console", params={"offset": first["offset"], "source": first["source"]}).json()
    assert second["source"] == "managed" and second["content"].startswith("managed model")
    third = client.get("/api/launcher/console", params={"offset": second["offset"], "source": second["source"]}).json()
    assert third["content"] == ""


def test_doctor_api_is_read_only_and_returns_report(client, monkeypatch):
    report = {"healthy": False, "summary": "GPU check failed", "checks": [], "recovery": []}
    monkeypatch.setattr(app_module, "doctor", lambda: report)
    assert client.get("/api/launcher/doctor").json() == report
    assert client.get("/api/launcher/state").json()["status"]["state"] == "stopped"


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
    identity = {"pid": 4321, "created_at": 1.0, "executable": "C:/bin/llama-server.exe",
                "argv": ["C:/bin/llama-server.exe"], "cwd": "C:/bin"}
    monkeypatch.setattr(launcher, "capture_process", lambda pid: identity)
    monkeypatch.setattr(launcher, "identity_matches", lambda record: True)
    store.set_running({"pid": 4321, "port": 9001, "config": {"name": "c1"},
                       "started_at": 1.0, "identity": identity, "log_path": str(tmp_path / "llama-server.log")})


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


@pytest.mark.parametrize("state,online,expected", [("running", True, "available"), ("running", False, "loading"), ("stopped", True, "no_model"), ("exited", True, "no_model")])
def test_layer_placement_stats_and_idle_polling(client, monkeypatch, state, online, expected):
    from history import LogFollower
    from placement import LayerPlacement
    parser = LayerPlacement()
    for msg in ["fitting params to device memory ...", "print_info: n_layer = 3",
                "load_tensors: layer 0 assigned to device CUDA9",
                "fitting params to free memory took 0.1 seconds",
                "print_info: n_layer = 1", "print_info: n_layer_all = 2",
                "load_tensors: layer 0 assigned to device CPU",
                "load_tensors: layer 1 assigned to device CUDA9",
                "load_tensors: layer 2 assigned to device CPU"]:
        parser.feed(msg)
    original = LogFollower.snapshot
    monkeypatch.setattr(LogFollower, "snapshot", lambda self: {
        **original(self), "managed": True, "available": True, "layer_parser": parser})
    monkeypatch.setattr(launcher.ServerManager, "status", lambda self: {"state": state, "config": {"model_path": "m.gguf"}})
    monkeypatch.setattr(app_module.GpuCollector, "collect", lambda self, pids: {"ok": False})
    levels = []
    def collect(self, level):
        levels.append(level)
        return {"online": online}
    monkeypatch.setattr(app_module.LlamaCollector, "collect", collect)
    result = client.get("/api/stats?lite=1").json()["layer_placement"]
    assert result["status"] == expected
    assert levels == ["none"]
    if expected == "available":
        assert result["total_layers"] == 1 and result["devices"] == [{"label": "CPU", "layers": 1}]
    else:
        assert result["devices"] == []
