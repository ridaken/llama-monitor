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

``--metrics`` and ``--log-file`` are intentionally *not* stored in ``flags`` —
they are injected by the launcher at known values. ``port`` is its own field so
it can never be removed.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from typing import Optional

HOME_DIR = os.path.join(os.path.expanduser("~"), ".llama-monitor")
STATE_PATH = os.path.join(HOME_DIR, "state.json")
# The log file llama-monitor tells launched servers to write (and then tails).
MANAGED_LOG = os.path.join(HOME_DIR, "llama-server.log")

DEFAULT_PORT = 8001

# Serialise reads/writes so concurrent requests can't corrupt the file.
_LOCK = threading.Lock()


def _ensure_dir() -> None:
    os.makedirs(HOME_DIR, exist_ok=True)


def _default_state() -> dict:
    return {
        "settings": {
            # Seed from PATH on first run; may be None if llama-server isn't installed
            # globally, in which case the UI shows a "set the path" warning.
            "llama_server_path": shutil.which("llama-server"),
            "models_dir": None,
            "default_port": DEFAULT_PORT,
        },
        "configs": [],
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
    return {"settings": settings, "configs": configs}


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


# --------------------------------------------------------------------------- #
# Settings                                                                     #
# --------------------------------------------------------------------------- #

def get_settings() -> dict:
    return load_state()["settings"]


def update_settings(**changes) -> dict:
    """Merge the given keys into settings and persist. Returns new settings."""
    state = load_state()
    for k, v in changes.items():
        if v is not None:
            state["settings"][k] = v
    return save_state(state)["settings"]


# --------------------------------------------------------------------------- #
# Saved configurations                                                         #
# --------------------------------------------------------------------------- #

def list_configs() -> list[dict]:
    return load_state()["configs"]


def get_config(name: str) -> Optional[dict]:
    for c in load_state()["configs"]:
        if c.get("name") == name:
            return c
    return None


def upsert_config(config: dict) -> list[dict]:
    """Create or overwrite a config by name. Returns the new config list."""
    name = (config.get("name") or "").strip()
    if not name:
        raise ValueError("config name is required")
    state = load_state()
    configs = [c for c in state["configs"] if c.get("name") != name]
    configs.append(config)
    state["configs"] = configs
    return save_state(state)["configs"]


def delete_config(name: str) -> list[dict]:
    state = load_state()
    state["configs"] = [c for c in state["configs"] if c.get("name") != name]
    return save_state(state)["configs"]
