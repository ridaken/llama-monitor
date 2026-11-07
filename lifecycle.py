"""Role-aware model orchestration, serialized with gaming and boot recovery."""
from __future__ import annotations

import copy
import socket
import threading
import time
import uuid

import flags
import psutil
import store
import observability
from launcher import LaunchError, resolve_binary
from verification import ManagedVerification, owns_port


class PreflightError(LaunchError):
    """Invalid inputs or unavailable listeners never qualify for CPU fallback."""


class _Unblocked:
    @staticmethod
    def state():
        return {"blocked": False}


class ManagedAuxiliary:
    """Gaming uses exactly one owner for managed or legacy embeddings."""

    def __init__(self, lifecycle, legacy):
        self.lifecycle, self.legacy = lifecycle, legacy

    @property
    def proc(self):
        return self.lifecycle.embeddings._proc or self.legacy.proc

    def snapshot(self, registration=None):
        managed = self.lifecycle.embeddings.snapshot()
        if managed:
            op = self.lifecycle.operation or {}
            managed.update(cpu=op.get("cpu", False), requested_config=op.get("config") or managed["config"])
            return managed
        if store.get_running("embeddings"):
            raise LaunchError("Cannot verify managed embeddings; refusing legacy discovery.")
        return self.legacy.snapshot(registration) if registration else None

    def alive(self, record):
        if record.get("managed"):
            current = self.lifecycle.embeddings.snapshot()
            return bool(current and current["pid"] == record["pid"])
        return self.legacy.alive(record)

    def stop(self, record):
        managed_child = (self.lifecycle.embeddings._proc is not None
                         or getattr(self.lifecycle.embeddings, "_adopted_pid", None) is not None)
        if (record and record.get("managed")) or (record is None and managed_child):
            self.lifecycle.stop_embeddings("gaming_suspension")
        else:
            self.legacy.stop(record)

    def restore(self, record):
        if not record.get("managed"):
            return self.legacy.restore(record)
        self.lifecycle.restore_embeddings(record)
        return self.lifecycle.embeddings.snapshot()


class ModelLifecycle:
    def __init__(self, primary, embeddings, gaming, *, reserved_ports=(), clock=time.time):
        self.primary, self.embeddings, self.gaming = primary, embeddings, gaming
        self.lock = gaming.lock
        self.clock = clock
        self.reserved_ports = set(reserved_ports)
        self.verifiers = {role: ManagedVerification(manager, _Unblocked())
                          for role, manager in (("primary", primary), ("embeddings", embeddings))}
        self.operation = copy.deepcopy(store.load_state()["embeddings"]["operation"])
        self.stop_event = threading.Event()
        self.thread = None
        self.legacy = gaming.auxiliary
        gaming.auxiliary = ManagedAuxiliary(self, self.legacy)
        gaming.lifecycle = self
        # The backend can die between persisting a child and publishing its
        # loading state. Adopt that invocation instead of launching it twice.
        record = store.get_running("embeddings")
        if record and embeddings.snapshot():
            if self.operation and self.operation["state"] == "stopped":
                embeddings.stop(reason="interrupted_stop")
            elif not self.operation or self.operation["state"] in ("waiting", "cpu_retry"):
                if not self.operation:
                    self._queue(record["config"])
                self.operation.update(state="loading", run_id=embeddings.run_id,
                                      deadline=self.clock() + 300, reason="Verifying adopted embeddings.")
                self._save()

    def _save(self):
        store.update_state(lambda state: state["embeddings"].update(operation=copy.deepcopy(self.operation)))

    def _state(self, state, reason):
        previous = self.operation.get("state")
        self.operation.update(state=state, reason=reason)
        self._save()
        recorder = getattr(self, "observability", None)
        if recorder and previous != state:
            context = {"role": "embeddings", "operation_id": self.operation["id"],
                       "model": observability.model_key(self.operation["config"].get("model_path")),
                       "run_id": self.operation.get("run_id"), "state": state}
            recorder.event("lifecycle", "embedding_" + state, "Embedding lifecycle state changed.",
                           "warning" if state == "failed" else "info", **context)
            if state == "cpu_retry":
                recorder.measure("cpu_fallback_attempts", model=context["model"], role="embeddings")
            if state == "ready" and self.operation.get("cpu"):
                recorder.measure("cpu_fallback_outcomes", model=context["model"], role="embeddings", run_id=context["run_id"])

    def _validate(self, config, manager, snapshot=None):
        config, binary = manager.validate(copy.deepcopy(config), **({"_snapshot": snapshot} if snapshot else {}))
        port = config["port"]
        if port in self.reserved_ports:
            raise LaunchError("Model port conflicts with the dashboard or inference proxy.")
        other = self.embeddings if manager is self.primary else self.primary
        other_config = other.current or {}
        if other_config.get("port") == port:
            raise LaunchError("Primary and embeddings must use different ports.")
        registration = store.load_state()["gaming"]["auxiliary"]
        if registration and registration["port"] == port:
            raise LaunchError("Port is registered for legacy embeddings; unregister it before managing this port.")
        current = manager.snapshot()
        if not current or current["port"] != port or not owns_port(current):
            self._check_port(port)
        return config

    @staticmethod
    def _check_port(port):
        try:
            if any(c.status == psutil.CONN_LISTEN and c.laddr.port == port
                   for c in psutil.net_connections(kind="tcp")):
                raise LaunchError("Model port is already in use or unavailable.")
            with socket.socket() as listener:
                # Ignore closed connections in TIME_WAIT, as llama-server does.
                # Check live listeners first: Windows permits shared binds with
                # SO_REUSEADDR, so a successful bind alone is not sufficient.
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(("127.0.0.1", port))
        except (OSError, psutil.Error) as exc:
            raise LaunchError("Model port is already in use or unavailable.") from exc

    def _allowed(self):
        if self.stop_event.is_set():
            raise LaunchError("Model orchestration is shutting down.")
        if self.gaming.transition:
            raise LaunchError("AI launch is blocked while gaming or recovery is pending.")

    def _queue(self, config, primary_run=None, *, recovery=False):
        self.operation = {"id": uuid.uuid4().hex, "config": copy.deepcopy(config),
                          "state": "waiting", "reason": "Waiting for primary readiness." if primary_run else "Queued for launch.",
                          "primary_run": primary_run, "cpu": False, "fallback_used": False,
                          "recovery": recovery, "run_id": None, "deadline": None,
                          "primary_deadline": self.clock() + 300}
        self._save()
        if getattr(self, "observability", None):
            self.observability.event("lifecycle", "embedding_queued", "Embedding launch queued.", role="embeddings",
                                     model=observability.model_key(config.get("model_path")),
                                     operation_id=self.operation["id"], state="waiting")

    def launch_primary(self, config=None):
        with self.lock:
            self._allowed()
            target = config if config is not None else self.primary.current
            if target is None:
                raise LaunchError("Nothing to restart — no configuration launched yet.")
            target = self._validate(target, self.primary)
            companion = None
            if self.embeddings.snapshot():
                companion = copy.deepcopy((self.operation or {}).get("config") or self.embeddings.current)
            elif self.operation and self.operation["state"] == "waiting":
                companion = copy.deepcopy(self.operation["config"])
            settings = store.get_settings("embeddings")
            if companion is None and settings["autostart_after_primary"]:
                companion = store.get_config(settings["default_config"], "embeddings")
            self.stop_embeddings("primary_replacement")
            try:
                result = self.primary.launch(target)
            except Exception:
                if companion:
                    self._queue(companion, "failed-primary")
                    self._state("waiting", "Primary launch failed; embeddings remain waiting.")
                raise
            if companion:
                self._queue(companion, self.primary.run_id)
            return result

    def launch_embeddings(self, config=None):
        with self.lock:
            self._allowed()
            target = config if config is not None else (self.operation or {}).get("config") or self.embeddings.current
            if target is None:
                settings = store.get_settings("embeddings")
                target = store.get_config(settings["default_config"], "embeddings")
            if not target:
                raise LaunchError("Choose an embedding configuration first.")
            target = self._validate(target, self.embeddings)
            self.stop_embeddings("replacement_launch")
            primary_run = self.primary.run_id if self.primary.snapshot() else None
            self._queue(target, primary_run)
            self.tick()

    def stop_embeddings(self, reason="manual_stop"):
        with self.lock:
            if self.operation:
                self._state("stopped", "Embedding launch cancelled.")
            try:
                self.embeddings.stop(reason=reason)
            except Exception as exc:
                if self.operation:
                    self._state("failed", f"Cannot confirm embedding shutdown: {exc}")
                raise

    def stop_primary(self):
        with self.lock:
            try:
                self.stop_embeddings()
            finally:
                self.primary.stop()

    def suspend(self):
        with self.lock:
            if self.operation:
                self._state("stopped", "Pending work cancelled by gaming or boot recovery.")

    def restore_embeddings(self, snapshot):
        with self.lock:
            self._queue(snapshot.get("requested_config") or snapshot["config"], recovery=True)
            self.operation["cpu"] = bool(snapshot.get("cpu"))
            self.operation["fallback_used"] = bool(snapshot.get("cpu"))
            self.operation["snapshot"] = copy.deepcopy(snapshot)
            self._save()
            self.tick()

    def cpu_config(self, config):
        advertised = flags.get_server_flags(resolve_binary(store.get_settings()["llama_server_path"]))
        supported = {alias for entry in advertised["flags"] for alias in entry["flags"]} if advertised["source"] == "help" else set()
        if not {"--device", "--gpu-layers", "--no-op-offload"} <= supported:
            raise LaunchError("Installed binary does not advertise the flags required for CPU-only fallback.")
        gpu_flags = {"--device", "-dev", "--gpu-layers", "--n-gpu-layers", "-ngl", "--main-gpu", "-mg",
                     "--tensor-split", "-ts", "--split-mode", "-sm", "--override-tensor", "-ot",
                     "--op-offload", "--no-op-offload", "--fit", "-fit", "--moe-cache-mib", "--rpc"}
        result = copy.deepcopy(config)
        result["flags"] = [f for f in result.get("flags", []) if f["flag"].split("=", 1)[0] not in gpu_flags]
        result["flags"] += [{"flag": "--device", "value": "none"}, {"flag": "--gpu-layers", "value": "0"},
                            {"flag": "--no-op-offload", "value": ""}]
        return result

    def _spawn(self):
        op = self.operation
        snapshot = op.get("snapshot") if not op.get("snapshot_attempted") else None
        try:
            config = snapshot["config"] if snapshot else self.cpu_config(op["config"]) if op["cpu"] else op["config"]
            self._validate(config, self.embeddings, snapshot)
        except Exception as exc:
            raise PreflightError(str(exc)) from exc
        op["snapshot_attempted"] = True
        if snapshot:
            self.embeddings.restore(snapshot)
        else:
            self.embeddings.launch(config)
        op.update(run_id=self.embeddings.run_id, deadline=self.clock() + 300)
        self._state("loading", "Loading on CPU." if op["cpu"] else "Loading embeddings.")

    def _failed(self, reason):
        op = self.operation
        # Cleanup must succeed before a second process can be started.
        try:
            self.embeddings.stop(reason="failed_load_cleanup")
        except Exception as exc:
            self._state("failed", f"Cannot verify failed-process cleanup: {exc}")
            return
        if store.get_settings("embeddings")["cpu_fallback"] and not op["fallback_used"]:
            op.update(cpu=True, fallback_used=True)
            self._state("cpu_retry", f"{reason} Retrying on CPU.")
        else:
            self._state("failed", reason)

    def tick(self):
        with self.lock:
            op = self.operation
            if self.stop_event.is_set() or not op or op["state"] in ("stopped", "failed"):
                return
            transition = self.gaming.transition
            if transition and not (op.get("recovery") and transition["phase"] == "restoring"):
                return
            if op["state"] == "ready":
                if not self.embeddings.snapshot():
                    self._state("failed", "Embedding server exited after becoming ready.")
                return
            if op["state"] == "waiting":
                if op.get("primary_failed"):
                    return
                if op["primary_run"]:
                    if op["primary_run"] != self.primary.run_id:
                        op["primary_failed"] = True
                        self._state("waiting", "Primary run changed or failed; embeddings remain waiting.")
                        return
                    ready = self.verifiers["primary"].readiness()
                    if not ready["ready"]:
                        if ready["state"] in ("failed", "stopped") or self.clock() >= op["primary_deadline"]:
                            op["primary_failed"] = True
                            self._state("waiting", "Primary failed or timed out; embeddings remain waiting.")
                        return
                # Revalidate queued paths and ports: these may change while waiting.
                try:
                    self._validate(op["config"], self.embeddings)
                except Exception as exc:
                    self._state("failed", str(exc))
                    return
            if op["state"] in ("waiting", "cpu_retry"):
                try:
                    self._spawn()
                except PreflightError as exc:
                    self._state("failed", str(exc))
                except Exception as exc:
                    if op["cpu"]:
                        self._state("failed", f"CPU fallback failed: {exc}")
                    else:
                        self._failed(str(exc))
                return
            if op["state"] == "loading":
                if self.embeddings.run_id != op["run_id"]:
                    self._state("failed", "Embedding run changed during loading.")
                    return
                ready = self.verifiers["embeddings"].readiness()
                if ready["ready"]:
                    self._state("ready", "Running on CPU." if op["cpu"] else "Embedding model ready.")
                elif ready["state"] in ("failed", "stopped") or self.clock() >= op["deadline"]:
                    # Identity errors must never trigger a fallback launch.
                    if "identity" in ready["reason"].lower() or "verify" in ready["reason"].lower():
                        self._state("failed", ready["reason"])
                    else:
                        self._failed(ready["reason"])

    def state(self):
        with self.lock:
            status = self.embeddings.status()
            op = self.operation or {}
            state = op.get("state", "loading" if status["state"] == "running" else "stopped")
            return {**status, "state": state, "ready": state == "ready", "operation_id": op.get("id"),
                    "run_id": self.embeddings.run_id, "reason": op.get("reason", "No embedding model running."),
                    "config_name": (op.get("config") or {}).get("name") or status.get("config_name"),
                    "config": op.get("config") or status.get("config"), "mode": "cpu" if op.get("cpu") else "configured"}

    def start(self):
        self.thread = threading.Thread(target=self._run, name="model-lifecycle", daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop_event.wait(.5):
            try:
                self.tick()
            except Exception as exc:
                with self.lock:
                    if self.operation:
                        self._state("failed", f"Cannot verify model lifecycle: {exc}")

    def close(self):
        with self.lock:
            self.stop_event.set()
            self.gaming.stop_event.set()
            if self.operation and self.operation["state"] in ("waiting", "cpu_retry"):
                self._state("stopped", "Pending work cancelled by backend shutdown.")
        if self.thread:
            self.thread.join(timeout=10)
