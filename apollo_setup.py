"""Install/roll back Apollo hooks, preserving existing configuration.

Run from the same Windows account as llama-monitor. --install requires write
access to Apollo's config directory; restart Apollo after installation/rollback.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import store


ROOT = Path(__file__).resolve().parent


def hook_config_path():
    return Path(store.HOME_DIR) / "apollo-hook.json"


def commands(config_path=None):
    path = str(config_path or hook_config_path())
    script = str(ROOT / "scripts" / "apollo-hook.ps1")
    return {action: subprocess.list2cmdline(["powershell.exe", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", script, "-Action", action, "-ConfigPath", path])
            for action in ("prepare", "session-ended")}


def write_hook_config(encrypted_token, port):
    store._ensure_dir()
    path = hook_config_path()
    content = {"token": encrypted_token, "python": sys.executable, "root": str(ROOT),
               "app": str(ROOT / "app.py"), "port": port, "url": f"http://127.0.0.1:{port}"}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(content, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return commands(path)


def configure(conf_text, apps, hook_commands):
    lines = conf_text.splitlines()
    existing = []
    index = None
    for i, line in enumerate(lines):
        if line.strip().startswith("global_prep_cmd") and "=" in line:
            if index is not None:
                raise ValueError("Duplicate global_prep_cmd entries; resolve before installation.")
            index = i
            existing = json.loads(line.partition("=")[2].strip())
    if not isinstance(existing, list):
        raise ValueError("Apollo global preparation commands must be an array.")
    if any("apollo-hook.ps1" in str(item) for item in existing):
        raise ValueError("llama-monitor hooks are already installed.")
    new = {"do": hook_commands["prepare"], "undo": hook_commands["session-ended"], "elevated": False}
    setting = "global_prep_cmd = " + json.dumps([new, *existing])
    if index is None:
        lines.append(setting)
    else:
        lines[index] = setting
    apps = json.loads(json.dumps(apps))
    targets = [app for app in apps["apps"] if app.get("name") in ("Desktop", "Steam Big Picture")]
    if len(targets) != 2:
        raise ValueError("Expected exactly one Desktop and one Steam Big Picture application.")
    for app in targets:
        app["terminate-on-pause"] = True
        app["exclude-global-prep-cmd"] = False
    return "\n".join(lines) + "\n", apps


def install(config_dir):
    config_dir = Path(config_dir).resolve()
    conf_path, apps_path = config_dir / "sunshine.conf", config_dir / "apps.json"
    config = hook_config_path()
    if not config.is_file():
        raise ValueError("Save and test Apollo settings in llama-monitor first.")
    backup = Path(store.HOME_DIR) / "apollo-hook-backup.json"
    if backup.exists():
        raise ValueError("An installation backup already exists; roll back before installing again.")
    originals = {str(p): p.read_bytes() for p in (conf_path, apps_path)}
    conf, apps = configure(originals[str(conf_path)].decode("utf-8-sig"),
                           json.loads(originals[str(apps_path)]), commands())
    updates = {str(conf_path): conf.encode(), str(apps_path): json.dumps(apps, indent=4).encode()}
    manifest = {"originals": {p: data.hex() for p, data in originals.items()},
                "installed": {p: hashlib.sha256(data).hexdigest() for p, data in updates.items()}}
    backup.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    try:
        for name, data in updates.items():
            Path(name).write_bytes(data)
    except Exception:
        for name, data in originals.items():
            Path(name).write_bytes(data)
        backup.unlink()
        raise
    # Installing hooks does not enable switching or touch any AI process.
    print(f"Hooks installed. Backup: {backup}. Restart Apollo, then validate in the dashboard.")


def rollback():
    backup = Path(store.HOME_DIR) / "apollo-hook-backup.json"
    manifest = json.loads(backup.read_text(encoding="utf-8"))
    for name, expected in manifest["installed"].items():
        if hashlib.sha256(Path(name).read_bytes()).hexdigest() != expected:
            raise ValueError("Apollo configuration changed after installation; restore the backup manually to preserve newer edits.")
    if store.load_state()["gaming"]["transition"]:
        raise ValueError("Cancel or finish pending AI restoration in the dashboard before rollback.")
    for name, data in manifest["originals"].items():
        Path(name).write_bytes(bytes.fromhex(data))
    store.update_gaming(enabled=False)
    backup.unlink()
    print("Previous Apollo configuration restored. Restart Apollo.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--install", action="store_true")
    action.add_argument("--rollback", action="store_true")
    parser.add_argument("--config-dir", default=r"C:\Program Files\Apollo\config")
    args = parser.parse_args()
    try:
        install(args.config_dir) if args.install else rollback()
    except Exception as exc:
        parser.exit(1, f"Setup failed: {exc}\n")
