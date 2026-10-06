import json
import threading
from pathlib import Path
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

import store
import windows_startup as startup
from background import log_config


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "HOME_DIR", str(tmp_path))
    monkeypatch.setattr(store, "STATE_PATH", str(tmp_path / "state.json"))
    python = tmp_path / "Python & tools/python.exe"
    python.parent.mkdir()
    python.touch()
    python.with_name("pythonw.exe").touch()
    monkeypatch.setattr(startup.sys, "executable", str(python))
    monkeypatch.setattr(startup, "current_identity", lambda: {"account": "PC\\Tom", "sid": "S-1-5-21-1-2-3-1001"})
    launches = []
    monkeypatch.setattr(startup, "launch_setup", lambda path: launches.append(path))
    model = {"id": "primary", "name": "Unsaved model", "port": 8001,
             "argv": ["server.exe", "--api-key", "private-test-key"], "config": {"name": "Unsaved model"}}
    coordinator = SimpleNamespace(lock=threading.RLock(), transition=None,
                                  capture_startup_models=lambda: [model])
    return startup.WindowsStartup(coordinator, 8500), launches, tmp_path


@pytest.mark.parametrize("mode,logon_type,boot", [("boot", "Password", True), ("logon", "InteractiveToken", False)])
def test_xml_supervision_identity_and_hidden_launch(mode, logon_type, boot):
    config = {"mode": mode, "sid": "S-1-5-21-1-2-3-1001", "pythonw": "C:/Python & tools/pythonw.exe", "port": 8500}
    xml = startup.task_xml(config)
    root = ET.fromstring(xml)
    ns = {"t": startup.NS}
    assert root.find("t:Principals/t:Principal/t:LogonType", ns).text == logon_type
    assert root.find("t:Principals/t:Principal/t:RunLevel", ns).text == "LeastPrivilege"
    assert (root.find("t:Triggers/t:BootTrigger", ns) is not None) == boot
    assert root.find("t:Settings/t:ExecutionTimeLimit", ns).text == "PT0S"
    assert root.find("t:Settings/t:MultipleInstancesPolicy", ns).text == "IgnoreNew"
    assert root.find("t:Settings/t:RestartOnFailure/t:Interval", ns).text == "PT1M"
    assert root.find("t:Actions/t:Exec/t:Command", ns).text == config["pythonw"]
    assert "--background" in root.find("t:Actions/t:Exec/t:Arguments", ns).text
    assert "password" not in xml.lower().replace("<logontype>password</logontype>", "")


def test_invalid_task_mode_is_rejected():
    with pytest.raises(ValueError, match="mode"):
        startup.task_xml({"mode": "invalid"})


@pytest.mark.skipif(startup.os.name != "nt", reason="Windows setup")
def test_prepare_opens_native_setup_without_sending_or_saving_password(setup):
    controller, launches, tmp = setup
    state = controller.setup()
    assert state["pending"] and not state["installed"]
    config = json.loads(launches[0].read_text())
    assert "password" not in config and "models" not in config
    assert config["account"] == "PC\\Tom"
    assert "private-test-key" not in json.dumps(state)
    assert "private-test-key" not in (tmp / "windows-startup-task.xml").read_text()
    assert "private-test-key" not in launches[0].read_text()
    with pytest.raises(ValueError, match="already pending"):
        controller.setup()


@pytest.mark.skipif(startup.os.name != "nt", reason="Windows setup")
def test_setup_success_applies_autostart_and_task_status(setup):
    controller, _, tmp = setup
    controller.setup()
    pending = store.load_state()["startup"]["pending"]
    controller.result_path().write_text(json.dumps({"operation": pending["operation"], "success": True}))
    state = controller.state()
    assert state["installed"] and state["autostart_models"] and not state["pending"]
    assert state["models"] == [{"id": "primary", "name": "Unsaved model", "port": 8001}]
    assert store.load_state()["startup"]["boot_marker"] == pending["boot_marker"]
    assert "private-test-key" not in json.dumps(state)


@pytest.mark.skipif(startup.os.name != "nt", reason="Windows setup")
def test_cancelled_windows_setup_does_not_change_existing_configuration(setup):
    controller, *_ = setup
    store.update_startup(installed=True, autostart_models=False)
    controller.setup()
    pending = store.load_state()["startup"]["pending"]
    controller.result_path().write_text(json.dumps({"operation": pending["operation"], "success": False, "error": "Cancelled"}))
    state = controller.state()
    assert state["installed"] and not state["autostart_models"]
    assert state["last_error"] == "Cancelled" and not state["pending"]


@pytest.mark.skipif(startup.os.name != "nt", reason="Windows setup")
def test_removal_disables_autostart_without_touching_processes(setup):
    controller, *_ = setup
    store.update_startup(installed=True, autostart_models=True)
    controller.setup(action="remove", autostart_models=False)
    pending = store.load_state()["startup"]["pending"]
    controller.result_path().write_text(json.dumps({"operation": pending["operation"], "success": True}))
    state = controller.state()
    assert not state["installed"] and not state["autostart_models"] and state["models"] == []


@pytest.mark.skipif(startup.os.name != "nt", reason="Windows setup")
def test_uac_cancel_and_empty_model_selection_are_reported(setup, monkeypatch):
    controller, *_ = setup
    def cancelled(path): raise ValueError("Windows setup cancelled")
    monkeypatch.setattr(startup, "launch_setup", cancelled)
    with pytest.raises(ValueError, match="cancelled"):
        controller.setup()
    assert not store.load_state()["startup"]["pending"]
    controller.coordinator.capture_startup_models = lambda: []
    with pytest.raises(ValueError, match="Launch the AI servers"):
        controller.setup()


def test_background_logging_is_bounded_and_has_no_console_handler(tmp_path):
    config = log_config(tmp_path)
    handler = config["handlers"]["file"]
    assert handler["maxBytes"] == 1024 * 1024 and handler["backupCount"] == 3
    assert all(h["class"] != "logging.StreamHandler" for h in config["handlers"].values())


def test_result_from_another_operation_is_ignored(setup):
    controller, *_ = setup
    store.update_startup(pending={"operation": "new", "created_at": startup.time.time()})
    controller.result_path().write_text(json.dumps({"operation": "old", "success": True}))
    controller.consume_result()
    assert store.load_state()["startup"]["pending"]["operation"] == "new"
