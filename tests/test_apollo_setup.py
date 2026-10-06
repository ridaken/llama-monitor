import json
from pathlib import Path

import pytest

import apollo_setup
import store


def test_configuration_preserves_commands_and_apps():
    conf = 'setting = keep\nglobal_prep_cmd = [{"do":"old-do","undo":"old-undo"}]\n'
    apps = {"apps": [{"name": "Desktop", "allow-client-commands": False},
                     {"name": "Steam Big Picture", "detached": ["steam://open/bigpicture"],
                      "prep-cmd": [{"undo": "steam://close/bigpicture"}]}, {"name": "Other"}]}
    updated, result = apollo_setup.configure(conf, apps, {"prepare": "prepare", "session-ended": "ended"})
    prep = json.loads(updated.split("global_prep_cmd = ")[1])
    assert prep[0]["do"] == "prepare" and prep[1]["do"] == "old-do"
    assert "setting = keep" in updated
    assert result["apps"][0]["terminate-on-pause"]
    assert result["apps"][0]["allow-client-commands"] is False
    assert result["apps"][1]["prep-cmd"] == apps["apps"][1]["prep-cmd"]
    assert result["apps"][2] == apps["apps"][2]
    assert "terminate-on-pause" not in apps["apps"][0]


def test_install_and_rollback_preserve_bytes_and_remain_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "HOME_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(store, "STATE_PATH", str(tmp_path / "state/state.json"))
    apollo_setup.write_hook_config("encrypted", 8500)
    config = tmp_path / "apollo"
    config.mkdir()
    original = b"test = keep\r\n"
    (config / "sunshine.conf").write_bytes(original)
    apps = {"apps": [{"name": "Desktop"}, {"name": "Steam Big Picture"}]}
    app_bytes = json.dumps(apps).encode()
    (config / "apps.json").write_bytes(app_bytes)
    apollo_setup.install(config)
    assert not store.load_state()["gaming"]["enabled"]
    assert "apollo-hook.ps1" in (config / "sunshine.conf").read_text()
    with pytest.raises(ValueError, match="backup already"):
        apollo_setup.install(config)
    apollo_setup.rollback()
    assert (config / "sunshine.conf").read_bytes() == original
    assert (config / "apps.json").read_bytes() == app_bytes


def test_rollback_refuses_overwriting_later_edits(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "HOME_DIR", str(tmp_path))
    monkeypatch.setattr(store, "STATE_PATH", str(tmp_path / "state.json"))
    manifest = {"originals": {}, "installed": {str(tmp_path / "sunshine.conf"): "old-hash"}}
    (tmp_path / "sunshine.conf").write_text("new user changes")
    (tmp_path / "apollo-hook-backup.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="changed after installation"):
        apollo_setup.rollback()
