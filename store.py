"""Persistent settings + saved launch configurations for llama-monitor.

Everything lives in a single JSON file at ``~/.llama-monitor/state.json`` so the
binary path, the last-used models directory and every saved launch config
survive across dashboard restarts. The managed log file llama-monitor writes for
launched servers lives next to it.

Shape::

    {
      "settings": {"llama_server_path": "...", "models_dir": "...", "default_port": 8001},
      "configs": [
        {"name": "Qwen3-27B", "model_path": "C:\\\\...\\\\model.gguf", "port": 8001,
         "flags": [{"flag": "-c", "value": "86000"}, {"flag": "-fa", "value": "on"}]}
      ]
    }

Each flag is ``{"flag": ..., "value": ..., "enabled"?: bool}``. ``enabled`` is
optional: absent (or ``true``) means the flag is passed at launch; ``false``
means the UI has toggled it off, so it is stored but skipped when building argv
(see :func:`launcher._flag_tokens`).

``--metrics`` and ``--log-file`` are intentionally *not* stored in ``flags`` —
they are injected by the launcher at known values. ``port`` is its own field so
it can never be removed.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import runtime
from typing import Optional

HOME_DIR = os.path.abspath(os.path.expanduser(os.environ.get(
    "LLAMA_MONITOR_DATA_DIR", os.path.join(os.path.expanduser("~"), ".llama-monitor"))))
STATE_PATH = os.path.join(HOME_DIR, "state.json")
# Legacy managed path / logical history cursor. New capture lives in logs/.
MANAGED_LOG = os.path.join(HOME_DIR, "llama-server.log")
PROMPTS_DIR = os.path.join(HOME_DIR, "prompts")

DEFAULT_PORT = 8001
ROLES = ("primary", "embeddings")


def validate_role(role):
    if role not in ROLES:
        raise ValueError("role must be primary or embeddings")
    return role

# Serialise reads/writes so concurrent requests can't corrupt the file.
_LOCK = threading.RLock()


def _ensure_dir() -> None:
    os.makedirs(HOME_DIR, exist_ok=True)


def _default_state() -> dict:
    return {
        "settings": {
            # Seed from PATH on first run; may be None if llama-server isn't installed
            # globally, in which case the UI shows a "set the path" warning.
            "llama_server_path": shutil.which("llama-server"),
            "models_dir": "/models" if runtime.containerized() else None,
            "default_port": DEFAULT_PORT,
            # Name of the config to auto-load on page open when no server is
            # running (the user's "default / favourite"). None when unset.
            "default_config": None,
            "autostart_default_model": False,
        },
        "configs": [],
        "proxy": {"enabled": False, "log_prompts": True, "log_responses": True},
        # The server this dashboard last launched, so a restarted dashboard can
        # re-adopt a still-running server instead of losing track of it. None
        # when nothing is launched (or after an explicit Stop).
        "running": None,
        "embeddings": {"default_config": None, "default_port": 8081,
                       "autostart_after_primary": False, "cpu_fallback": False,
                       "running": None, "operation": None},
        "gaming": {"enabled": False, "apollo_url": "https://localhost:47990",
                   "credentials": None, "certificate_sha256": None,
                   "auxiliary": None, "transition": None},
        "startup": {"installed": False, "mode": "boot", "autostart_models": False,
                    "models": [], "boot_marker": None, "pending": None,
                    "last_error": None, "last_result": None, "task_name": None},
    }


def _normalise(state: dict) -> dict:
    """Fill in any missing keys so callers can rely on the full shape."""
    base = _default_state()
    settings = {**base["settings"], **(state.get("settings") or {})}
    # Don't let a stored null binary path mask a now-installed one.
    if not settings.get("llama_server_path"):
        settings["llama_server_path"] = shutil.which("llama-server")
    if not settings.get("default_port"):
        settings["default_port"] = DEFAULT_PORT
    configs = state.get("configs")
    if not isinstance(configs, list):
        configs = []
    running = state.get("running")
    if not isinstance(running, dict):
        running = None
    gaming = {**base["gaming"], **(state.get("gaming") or {})}
    startup = {**base["startup"], **(state.get("startup") or {})}
    proxy = {**base["proxy"], **(state.get("proxy") or {})}
    embeddings = {**base["embeddings"], **(state.get("embeddings") or {})}
    return {"settings": settings, "configs": configs, "running": running, "embeddings": embeddings,
            "gaming": gaming, "startup": startup, "proxy": proxy}


def load_state() -> dict:
    with _LOCK:
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return _normalise(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            return _normalise({})
        except Exception:
            return _normalise({})


def save_state(state: dict) -> dict:
    state = _normalise(state)
    with _LOCK:
        _ensure_dir()
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_PATH)  # atomic on the same filesystem
    return state


def update_state(change) -> dict:
    """Apply a read-modify-write transaction under one lock."""
    with _LOCK:
        state = load_state()
        change(state)
        return save_state(state)


def update_gaming(**changes) -> dict:
    return update_state(lambda state: state["gaming"].update(changes))["gaming"]


def update_startup(**changes) -> dict:
    return update_state(lambda state: state["startup"].update(changes))["startup"]


def get_proxy() -> dict:
    return load_state()["proxy"]


def update_proxy(**changes) -> dict:
    return update_state(lambda state: state["proxy"].update(changes))["proxy"]


# --------------------------------------------------------------------------- #
# Settings                                                                     #
# --------------------------------------------------------------------------- #

def get_settings(role="primary") -> dict:
    validate_role(role)
    state = load_state()
    return state["settings"] if role == "primary" else {**state["settings"], **{
        key: value for key, value in state["embeddings"].items() if key not in ("running", "operation")}}


def update_settings(role="primary", **changes) -> dict:
    """Merge the given keys into settings and persist. Returns new settings."""
    if "autostart_default_model" in changes and type(changes["autostart_default_model"]) is not bool:
        raise ValueError("autostart_default_model must be a boolean")
    validate_role(role)
    for key in ("autostart_after_primary", "cpu_fallback"):
        if key in changes and type(changes[key]) is not bool:
            raise ValueError(f"{key} must be a boolean")
    key = "settings" if role == "primary" else "embeddings"
    update_state(lambda state: state[key].update(
        {k: v for k, v in changes.items() if v is not None}))
    return get_settings(role)


# --------------------------------------------------------------------------- #
# Currently-launched server (survives a dashboard restart)                     #
# --------------------------------------------------------------------------- #

def get_running(role="primary") -> Optional[dict]:
    """The record of the server this dashboard launched, or None."""
    validate_role(role)
    state = load_state()
    return state.get("running") if role == "primary" else state["embeddings"]["running"]


def set_running(record: Optional[dict], role="primary") -> None:
    """Persist (or clear, with None) the launched-server record."""
    validate_role(role)
    def change(state):
        target = state if role == "primary" else state["embeddings"]
        target["running"] = record if isinstance(record, dict) else None
    update_state(change)


# --------------------------------------------------------------------------- #
# Saved configurations                                                         #
# --------------------------------------------------------------------------- #

def list_configs(role="primary") -> list[dict]:
    validate_role(role)
    return [c for c in load_state()["configs"] if c.get("role", "primary") == role]


def get_config(name: str, role="primary") -> Optional[dict]:
    for c in list_configs(role):
        if c.get("name") == name:
            return c
    return None


def upsert_config(config: dict, role="primary") -> list[dict]:
    """Create or overwrite a config by name. Returns the new config list."""
    name = (config.get("name") or "").strip()
    if not name:
        raise ValueError("config name is required")
    validate_role(role)
    config = {**config, "name": name}
    if role != "primary":
        config["role"] = role
    else:
        config.pop("role", None)
    def change(state):
        state["configs"] = [c for c in state["configs"]
                            if c.get("name") != name or c.get("role", "primary") != role] + [config]
    update_state(change)
    return list_configs(role)


def delete_config(name: str, role="primary") -> list[dict]:
    validate_role(role)
    def change(state):
        state["configs"] = [c for c in state["configs"]
                            if c.get("name") != name or c.get("role", "primary") != role]
        settings = state["settings" if role == "primary" else "embeddings"]
        if settings.get("default_config") == name:
            settings["default_config"] = None
    update_state(change)
    return list_configs(role)


def set_default_config(name: Optional[str], role="primary") -> dict:
    """Set (or clear, with a falsy name) the auto-load default config.

    Raises ValueError if a non-empty name doesn't match a saved config.
    """
    name = (name or "").strip() or None
    validate_role(role)
    def change(state):
        if name is not None and not any(c.get("name") == name and c.get("role", "primary") == role for c in state["configs"]):
            raise ValueError(f"no such configuration: {name}")
        state["settings" if role == "primary" else "embeddings"]["default_config"] = name
    update_state(change)
    return get_settings(role)
