"""Tests for store.py — settings + saved-config persistence. Each test runs
against a temp state file so nothing touches the real ~/.llama-monitor."""

import json

import pytest

import store


@pytest.fixture(autouse=True)
def temp_state(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "HOME_DIR", str(tmp_path))
    monkeypatch.setattr(store, "STATE_PATH", str(tmp_path / "state.json"))
    # Deterministic auto-detect: pretend llama-server isn't on PATH.
    monkeypatch.setattr(store.shutil, "which", lambda name: None)
    return tmp_path


def test_defaults_when_no_file():
    s = store.get_settings()
    assert s["default_port"] == store.DEFAULT_PORT
    assert s["llama_server_path"] is None
    assert store.list_configs() == []


def test_update_settings_persists(temp_state):
    store.update_settings(llama_server_path="C:/tools/llama-server.exe", models_dir="C:/models")
    s = store.get_settings()
    assert s["llama_server_path"] == "C:/tools/llama-server.exe"
    assert s["models_dir"] == "C:/models"
    # Written through to disk in the documented shape.
    on_disk = json.loads((temp_state / "state.json").read_text(encoding="utf-8"))
    assert on_disk["settings"]["models_dir"] == "C:/models"


def test_update_settings_ignores_none():
    store.update_settings(llama_server_path="C:/a/llama-server.exe")
    store.update_settings(llama_server_path=None, models_dir="C:/models")
    s = store.get_settings()
    assert s["llama_server_path"] == "C:/a/llama-server.exe"  # not overwritten by None
    assert s["models_dir"] == "C:/models"


def test_upsert_creates_then_overwrites_by_name():
    store.upsert_config({"name": "cfg", "model_path": "C:/a.gguf", "port": 8001, "flags": []})
    store.upsert_config({"name": "cfg", "model_path": "C:/b.gguf", "port": 9000, "flags": []})
    configs = store.list_configs()
    assert len(configs) == 1                       # overwrite, not append
    assert configs[0]["model_path"] == "C:/b.gguf"
    assert configs[0]["port"] == 9000


def test_upsert_requires_name():
    with pytest.raises(ValueError):
        store.upsert_config({"name": "", "model_path": "C:/a.gguf"})


def test_get_and_delete_config():
    store.upsert_config({"name": "keep", "model_path": "C:/k.gguf", "port": 8001, "flags": []})
    store.upsert_config({"name": "drop", "model_path": "C:/d.gguf", "port": 8001, "flags": []})
    assert store.get_config("keep")["model_path"] == "C:/k.gguf"

    remaining = store.delete_config("drop")
    assert [c["name"] for c in remaining] == ["keep"]
    assert store.get_config("drop") is None


def test_normalise_recovers_from_corrupt_file(temp_state):
    (temp_state / "state.json").write_text("{ not valid json", encoding="utf-8")
    # Should fall back to defaults rather than raising.
    assert store.list_configs() == []
    assert store.get_settings()["default_port"] == store.DEFAULT_PORT


def test_default_config_set_clear_and_validate():
    store.upsert_config({"name": "a", "model_path": "C:/a.gguf", "port": 8001, "flags": []})
    store.set_default_config("a")
    assert store.get_settings()["default_config"] == "a"
    store.set_default_config("")                 # clear
    assert store.get_settings()["default_config"] is None
    with pytest.raises(ValueError):              # must reference an existing config
        store.set_default_config("nope")


def test_delete_clears_default():
    store.upsert_config({"name": "a", "model_path": "C:/a.gguf", "port": 8001, "flags": []})
    store.set_default_config("a")
    store.delete_config("a")
    assert store.get_settings()["default_config"] is None


def test_running_record_round_trip(temp_state):
    assert store.get_running() is None              # default
    rec = {"pid": 4321, "port": 9001, "config": {"name": "c1"}}
    store.set_running(rec)
    assert store.get_running() == rec
    # Persisted alongside settings/configs without clobbering them.
    on_disk = json.loads((temp_state / "state.json").read_text(encoding="utf-8"))
    assert on_disk["running"]["port"] == 9001
    store.set_running(None)
    assert store.get_running() is None
