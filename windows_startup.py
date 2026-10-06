"""One-time Windows startup installer; credentials stay in the Windows dialog."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

import psutil

import store


ROOT = Path(__file__).resolve().parent
DESCRIPTION = "Managed by llama-monitor startup setup"
NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def powershell():
    return str(Path(os.environ.get("SystemRoot", r"C:\Windows")) /
               "System32/WindowsPowerShell/v1.0/powershell.exe")


def current_identity():
    result = subprocess.run([powershell(), "-NoProfile", "-NonInteractive", "-Command",
        "$i=[Security.Principal.WindowsIdentity]::GetCurrent();"
        "@{account=$i.Name;sid=$i.User.Value}|ConvertTo-Json -Compress"],
        capture_output=True, text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise ValueError("Cannot determine the Windows account for startup.")
    return json.loads(result.stdout)


def task_xml(config):
    """Generate a least-privilege, supervised task with no password in XML."""
    if config["mode"] not in ("boot", "logon"):
        raise ValueError("Startup mode must be boot or logon.")
    ET.register_namespace("", NS)
    root = ET.Element(f"{{{NS}}}Task", version="1.4")
    def child(parent, name, value=None, **attributes):
        item = ET.SubElement(parent, f"{{{NS}}}{name}", attributes)
        if value is not None:
            item.text = str(value)
        return item
    registration = child(root, "RegistrationInfo")
    child(registration, "Description", DESCRIPTION)
    triggers = child(root, "Triggers")
    if config["mode"] == "boot":
        boot = child(triggers, "BootTrigger")
        child(boot, "Enabled", "true")
        child(boot, "Delay", "PT30S")
    logon = child(triggers, "LogonTrigger")
    child(logon, "Enabled", "true")
    child(logon, "UserId", config["sid"])
    child(logon, "Delay", "PT10S")
    principal = child(child(root, "Principals"), "Principal", id="Owner")
    child(principal, "UserId", config["sid"])
    child(principal, "LogonType", "Password" if config["mode"] == "boot" else "InteractiveToken")
    child(principal, "RunLevel", "LeastPrivilege")
    settings = child(root, "Settings")
    for name, value in (("MultipleInstancesPolicy", "IgnoreNew"), ("DisallowStartIfOnBatteries", "false"),
                        ("StopIfGoingOnBatteries", "false"), ("StartWhenAvailable", "true"),
                        ("RunOnlyIfNetworkAvailable", "false"), ("Enabled", "true"),
                        ("ExecutionTimeLimit", "PT0S")):
        child(settings, name, value)
    restart = child(settings, "RestartOnFailure")
    child(restart, "Interval", "PT1M")
    child(restart, "Count", "999")
    execution = child(child(root, "Actions", Context="Owner"), "Exec")
    child(execution, "Command", config["pythonw"])
    child(execution, "Arguments", subprocess.list2cmdline([
        str(ROOT / "app.py"), "--background", "--port", str(config["port"]),
        "--host", "127.0.0.1", "--llama-url", "http://localhost:8080"]))
    child(execution, "WorkingDirectory", str(ROOT))
    return ET.tostring(root, encoding="unicode")


def launch_setup(config_path):
    params = subprocess.list2cmdline(["-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                     str(ROOT / "scripts/windows-startup.ps1"), "-ConfigPath", str(config_path)])
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.ShellExecuteW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                   ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
    shell.ShellExecuteW.restype = ctypes.c_void_p
    result = shell.ShellExecuteW(None, "runas", powershell(), params, str(ROOT), 0)
    if not result or result <= 32:
        raise ValueError("Windows startup setup was cancelled or could not be opened.")


class WindowsStartup:
    def __init__(self, coordinator, port):
        self.coordinator = coordinator
        self.port = port

    def result_path(self):
        return Path(store.HOME_DIR) / "windows-startup-result.json"

    def consume_result(self):
        with self.coordinator.lock:
            settings = store.load_state()["startup"]
            pending = settings["pending"]
            if not pending:
                return
            try:
                result = json.loads(self.result_path().read_text(encoding="utf-8-sig"))
            except (FileNotFoundError, ValueError):
                if time.time() - pending["created_at"] > 600:
                    store.update_startup(pending=None, last_error="Windows setup did not finish. Try again.")
                return
            if result.get("operation") != pending["operation"]:
                return
            if result.get("success") is True:
                installed = pending["action"] == "install"
                def apply(state):
                    state["startup"].update(installed=installed, mode=pending["mode"],
                        task_name=pending["task_name"], autostart_models=installed and pending["autostart_models"],
                        models=pending["models"] if installed else [], boot_marker=pending["boot_marker"],
                        pending=None, last_error=None, last_result="Setup completed; applies on next boot or sign-in.")
                    embedding = next((s for s in pending["models"] if s["id"] == "embeddings"), None)
                    if installed and embedding and not state["gaming"]["auxiliary"]:
                        state["gaming"]["auxiliary"] = {"executable": embedding["executable"], "port": embedding["port"]}
                store.update_state(apply)
            else:
                store.update_startup(pending=None, last_error=result.get("error") or "Windows setup failed.")

    def state(self):
        self.consume_result()
        settings = store.load_state()["startup"]
        return {"supported": os.name == "nt", "installed": settings["installed"],
                "mode": settings["mode"], "autostart_models": settings["autostart_models"],
                "models": [{"id": s["id"], "name": s["name"], "port": s["port"]} for s in settings["models"]],
                "pending": settings["pending"] is not None, "last_error": settings["last_error"],
                "last_result": settings["last_result"], "task_name": settings["task_name"],
                "log_path": str(Path(store.HOME_DIR) / "backend.log")}

    def setup(self, *, action="install", mode="boot", autostart_models=True):
        if os.name != "nt":
            raise ValueError("Automatic Windows startup setup is available only on Windows.")
        if action not in ("install", "remove") or mode not in ("boot", "logon"):
            raise ValueError("Invalid startup setup action or mode.")
        if not isinstance(autostart_models, bool):
            raise ValueError("autostart_models must be a boolean.")
        with self.coordinator.lock:
            self.consume_result()
            if store.load_state()["startup"]["pending"]:
                raise ValueError("A Windows setup dialog is already pending.")
            if self.coordinator.transition:
                raise ValueError("Finish or cancel model switching before changing Windows startup.")
            identity = current_identity()
            pythonw = Path(sys.executable).with_name("pythonw.exe")
            if not pythonw.is_file():
                raise ValueError("pythonw.exe was not found beside this Python interpreter.")
            models = self.coordinator.capture_startup_models() if action == "install" and autostart_models else []
            if action == "install" and autostart_models and not models:
                raise ValueError("Launch the AI servers to remember at boot, or turn off model autostart.")
            operation = uuid.uuid4().hex
            task_name = "llama-monitor-" + hashlib.sha256(identity["sid"].encode()).hexdigest()[:8]
            config = {**identity, "operation": operation, "action": action, "mode": mode,
                      "pythonw": str(pythonw), "port": self.port, "task_name": task_name,
                      "description": DESCRIPTION, "result_path": str(self.result_path())}
            store._ensure_dir()
            config_path = Path(store.HOME_DIR) / "windows-startup-setup.json"
            xml_path = Path(store.HOME_DIR) / "windows-startup-task.xml"
            xml_path.write_text(task_xml(config), encoding="utf-8")
            config["xml_path"] = str(xml_path)
            config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
            store.update_startup(pending={"operation": operation, "created_at": time.time(),
                "boot_marker": psutil.boot_time(),
                "action": action, "mode": mode, "task_name": task_name,
                "autostart_models": autostart_models, "models": models}, last_error=None)
            try:
                launch_setup(config_path)
            except Exception:
                store.update_startup(pending=None, last_error="Windows setup was cancelled or could not be started.")
                raise
            return self.state()
