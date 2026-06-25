"""Launch and manage a llama-server child process from llama-monitor.

The dashboard owns three flags so it can monitor whatever it launches:
``--port`` (so it knows where to poll), ``--metrics`` (throughput/KV gauges) and
``--log-file`` pointed at :data:`store.MANAGED_LOG` (activity detection + exact
per-request stats). The user supplies the model path, the port value and any
other flags; those three are injected at launch.

Single-instance by design: launching while a server is running stops the old one
first. On a successful launch the manager calls the injected ``retarget``
callback so the dashboard immediately starts polling the new server.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from typing import Callable, Optional

import store

# Give a terminated server a moment to exit before escalating to kill.
_STOP_GRACE_SECS = 5.0


class LaunchError(Exception):
    """Raised when a launch request is invalid (bad binary/model/port)."""


def _flag_tokens(flags: list[dict]) -> list[str]:
    """Expand [{flag, value}, ...] into a flat argv list.

    A blank/missing value yields a bare switch (e.g. ``--mlock``); otherwise the
    value follows as its own token (e.g. ``-c 86000``).
    """
    out: list[str] = []
    for f in flags or []:
        flag = (f.get("flag") or "").strip()
        if not flag:
            continue
        out.append(flag)
        value = f.get("value")
        if value is not None and str(value).strip() != "":
            out.append(str(value).strip())
    return out


def resolve_binary(path: Optional[str]) -> Optional[str]:
    """Return an executable path for the configured binary, or None if invalid.

    Accepts an absolute path (must exist) or a bare command name resolved via
    PATH. A missing/blank value resolves the default ``llama-server``.
    """
    if path and (os.path.sep in path or (os.path.altsep and os.path.altsep in path)):
        return path if os.path.isfile(path) else None
    return shutil.which(path or "llama-server")


class ServerManager:
    """Owns the lifecycle of a single launched llama-server process."""

    def __init__(self, retarget: Callable[[str, str, int], None]):
        # retarget(url, log_path, port) repoints the dashboard's collectors.
        self._retarget = retarget
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self.current: Optional[dict] = None   # the config we launched
        self.started_at: Optional[float] = None
        self.exit_code: Optional[int] = None
        self.last_error: Optional[str] = None
        # Tells an explicit Stop apart from a crash: only a process that exits on
        # its own is reported as "exited (code N)".
        self._stopped_by_user = False

    # -- state -------------------------------------------------------------- #

    def _refresh(self) -> None:
        """Reap the process if it has exited, updating status fields."""
        if self._proc is not None and self._proc.poll() is not None:
            self.exit_code = self._proc.returncode
            self._proc = None

    def status(self) -> dict:
        with self._lock:
            self._refresh()
            if self._proc is not None:
                state = "running"
            elif self._stopped_by_user:
                state = "stopped"
            elif self.current is not None and self.exit_code is not None:
                state = "exited"
            else:
                state = "stopped"
            return {
                "state": state,
                "config_name": (self.current or {}).get("name"),
                "pid": self._proc.pid if self._proc else None,
                "exit_code": self.exit_code,
                "started_at": self.started_at,
                "last_error": self.last_error,
            }

    # -- argv --------------------------------------------------------------- #

    def build_argv(self, config: dict, binary: str) -> list[str]:
        model_path = (config.get("model_path") or "").strip()
        port = config.get("port")
        argv = [binary, "-m", model_path]
        argv += _flag_tokens(config.get("flags") or [])
        # Managed flags, always at known values, appended last so they win.
        argv += ["--port", str(int(port))]
        argv += ["--metrics"]
        argv += ["--log-file", store.MANAGED_LOG]
        return argv

    # -- lifecycle ---------------------------------------------------------- #

    def launch(self, config: dict) -> dict:
        settings = store.get_settings()
        binary = resolve_binary(settings.get("llama_server_path"))
        if not binary:
            raise LaunchError(
                "llama-server executable not found — set its path before launching."
            )

        model_path = (config.get("model_path") or "").strip()
        if not model_path:
            raise LaunchError("No model file selected.")
        if not os.path.isfile(model_path):
            raise LaunchError(f"Model file not found: {model_path}")
        if not model_path.lower().endswith(".gguf"):
            raise LaunchError("Model file must be a .gguf file.")

        try:
            port = int(config.get("port"))
        except (TypeError, ValueError):
            raise LaunchError("Port must be a number.")
        if not (1 <= port <= 65535):
            raise LaunchError("Port must be between 1 and 65535.")
        config = {**config, "port": port}

        argv = self.build_argv(config, binary)

        with self._lock:
            self._refresh()
            if self._proc is not None:
                self._stop_locked()
            store._ensure_dir()
            # Truncate the managed log so the tailer starts clean for this run.
            try:
                open(store.MANAGED_LOG, "w").close()
            except Exception:
                pass
            try:
                self._proc = subprocess.Popen(
                    argv,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    cwd=os.path.dirname(binary) or None,
                )
            except Exception as e:
                self._proc = None
                self.last_error = str(e)
                raise LaunchError(f"Failed to start llama-server: {e}")

            self.current = config
            self.started_at = time.time()
            self.exit_code = None
            self.last_error = None
            self._stopped_by_user = False

        # Repoint the dashboard at the freshly launched server.
        self._retarget(f"http://127.0.0.1:{port}", store.MANAGED_LOG, port)
        return self.status()

    def stop(self) -> dict:
        with self._lock:
            self._stop_locked()
            self._stopped_by_user = True
        return self.status()

    def _stop_locked(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=_STOP_GRACE_SECS)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=_STOP_GRACE_SECS)
        except Exception as e:
            self.last_error = str(e)
        finally:
            self.exit_code = proc.returncode
            self._proc = None

    def restart(self) -> dict:
        if self.current is None:
            raise LaunchError("Nothing to restart — no configuration launched yet.")
        return self.launch(self.current)
