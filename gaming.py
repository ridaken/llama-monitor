"""Persistent, connection-driven AI process suspension and bounded restoration."""
from __future__ import annotations

import copy
import threading
import time

import httpx
import psutil

import store
from apollo import ApolloClient
from launcher import LaunchError, resolve_binary
from processes import AuxiliaryServer, identity_matches


class GamingCoordinator:
    def __init__(self, manager, *, auxiliary=None, apollo=None, clock=time.time, health=None):
        self.manager = manager
        self.auxiliary = auxiliary or AuxiliaryServer()
        self.apollo = apollo or ApolloClient()
        self.clock = clock
        self.health = health
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread = None
        self.startup_results = None
        self.generation = 0
        self.connected = None
        self.integration_error = None
        self.transition = copy.deepcopy(store.load_state()["gaming"]["transition"])
        # A stopped/restarted backend must establish fresh connection truth
        # before making any decision to restore a persisted transition.
        self.reconciled = False

    @staticmethod
    def _health(port, pid=None):
        try:
            if pid is not None and not any(c.status == psutil.CONN_LISTEN and c.laddr.port == port
                                          for c in psutil.Process(pid).net_connections(kind="tcp")):
                return False
            with httpx.Client(timeout=1, trust_env=False) as client:
                response = client.get(f"http://127.0.0.1:{port}/health")
                return response.status_code == 200 and response.json().get("status") == "ok"
        except (httpx.HTTPError, ValueError, psutil.Error):
            return False

    def _save(self):
        store.update_gaming(transition=copy.deepcopy(self.transition))

    def state(self):
        with self.lock:
            settings = store.load_state()["gaming"]
            transition = self.transition or {}
            servers = transition.get("servers", [])
            phase = transition.get("phase", "normal")
            deadline = transition.get("deadline")
            return {"enabled": settings["enabled"], "phase": phase,
                    "reason": transition.get("reason"),
                    "blocked": phase != "normal", "connected_clients": None if self.connected is None else len(self.connected),
                    "countdown": max(0, int(deadline - self.clock() + .999)) if deadline else None,
                    "integration_error": self.integration_error,
                    "servers": [{"id": s["id"], "name": s["name"], "port": s["port"],
                                 "status": s["status"], "attempts": s["attempts"], "error": s.get("error")}
                                for s in servers],
                    "apollo_url": settings["apollo_url"],
                    "credentials_saved": bool(settings.get("credentials")),
                    "certificate_sha256": settings.get("certificate_sha256"),
                    "auxiliary": settings.get("auxiliary")}

    def capture_startup_models(self):
        """Remember actual invocations; never substitute a named default."""
        with self.lock:
            snapshots = []
            primary = self.manager.snapshot()
            if primary:
                snapshots.append(primary)
            elif store.get_running():
                raise LaunchError("Restart without an explicit watch target to adopt the managed model first.")
            settings = store.load_state()["gaming"]
            registration = settings["auxiliary"]
            if not registration:
                binary = resolve_binary(store.get_settings().get("llama_server_path"))
                registration = {"executable": binary, "port": 8081} if binary else None
            if registration:
                auxiliary = self.auxiliary.snapshot(registration)
                if auxiliary:
                    if primary and primary["pid"] == auxiliary["pid"]:
                        raise LaunchError("Embedding registration matches the primary model.")
                    snapshots.append(auxiliary)
            return sorted(snapshots, key=lambda s: s["created_at"])

    def schedule_boot_models(self, boot_marker=None):
        """Queue once per Windows boot; backend restarts respect explicit Stop."""
        with self.lock:
            marker = psutil.boot_time() if boot_marker is None else boot_marker
            settings = store.load_state()["startup"]
            if not settings["installed"] or not settings["autostart_models"] or settings["boot_marker"] == marker:
                return
            queued = []
            if self.transition is None:
                for snapshot in settings["models"]:
                    if snapshot["id"] == "primary":
                        live = self.manager.snapshot()
                    else:
                        registration = store.load_state()["gaming"]["auxiliary"]
                        live = self.auxiliary.snapshot(registration) if registration else None
                    if live:
                        continue
                    queued.append(copy.deepcopy(snapshot))
                if queued:
                    self.transition = {"reason": "startup", "phase": "countdown", "deadline": self.clock(),
                        "prepared_until": None,
                        "servers": [{**s, "original": copy.deepcopy(s), "live": None,
                                     "status": "stopped", "attempts": 0, "error": None} for s in queued]}
            def persist(state):
                state["startup"].update(boot_marker=marker, last_result=(
                    "AI boot startup queued." if queued else "Existing servers or pending model recovery take priority."))
                state["gaming"]["transition"] = copy.deepcopy(self.transition)
            store.update_state(persist)

    def primary_config(self):
        with self.lock:
            entry = next((s for s in (self.transition or {}).get("servers", [])
                          if s["id"] == "primary" and s["status"] != "cancelled"), None)
            return copy.deepcopy(entry["original"].get("config")) if entry else None

    def start(self):
        if self.startup_results:
            self.startup_results()
        self.schedule_boot_models()
        self.thread = threading.Thread(target=self._run, name="gaming-coordinator", daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        self.wake_event.set()
        if self.thread:
            self.thread.join(timeout=45)

    def _run(self):
        while not self.stop_event.is_set():
            try:
                if self.startup_results:
                    self.startup_results()
                self.tick()
            except Exception:
                # Avoid printing credentials or invocation arguments in errors.
                with self.lock:
                    self.integration_error = "Switching encountered an error; check registered process access."
            self.wake_event.wait(2)
            self.wake_event.clear()

    def launch(self, config=None):
        with self.lock:
            if self.transition:
                raise LaunchError("AI launch is blocked while Moonlight switching or recovery is pending.")
            auxiliary = store.load_state()["gaming"]["auxiliary"]
            target = config if config is not None else self.manager.current
            try:
                target_port = int(target.get("port")) if target else None
            except (ValueError, TypeError):
                target_port = None  # The existing launcher reports invalid input.
            if auxiliary and target_port == auxiliary["port"]:
                raise LaunchError("This port is registered for embeddings; choose a different model port.")
            return self.manager.restart() if config is None else self.manager.launch(config)

    def stop_server(self, server_id="primary"):
        with self.lock:
            if server_id not in ("primary", "embeddings"):
                raise LaunchError("Unknown registered server.")
            entry = next((s for s in (self.transition or {}).get("servers", []) if s["id"] == server_id), None)
            if server_id == "primary":
                self.manager.stop()
            elif entry and (entry.get("live") or getattr(self.auxiliary, "proc", None)):
                self.auxiliary.stop(entry.get("live"))
            if entry:
                entry.update(status="cancelled", live=None, error=None)
                self._save()
            return self.state()

    def cancel(self):
        """Forget automatic restore intent, without starting or killing anything."""
        with self.lock:
            if self.transition:
                for entry in self.transition["servers"]:
                    entry.update(status="cancelled", error=None)
                self._save()
                if self.transition["phase"] == "failed" and self.connected == []:
                    self.transition = None
                    self._save()
            return self.state()

    def prepare(self):
        with self.lock:
            settings = store.load_state()["gaming"]
            if not settings["enabled"]:
                raise LaunchError("Moonlight integration is disabled.")
            self.generation += 1
            if (self.transition is None or
                    (self.transition["phase"] == "failed"
                     and not any(s["status"] == "stop_failed" for s in self.transition["servers"]))):
                snapshots = []
                primary = self.manager.snapshot()
                if primary is None and store.get_running():
                    raise LaunchError("The recorded managed server could not be adopted; refusing to skip it.")
                if primary:
                    snapshots.append(primary)
                if settings["auxiliary"]:
                    auxiliary = self.auxiliary.snapshot(settings["auxiliary"])
                    if auxiliary:
                        if primary and primary["pid"] == auxiliary["pid"]:
                            raise LaunchError("The primary and embedding registrations match the same process.")
                        snapshots.append(auxiliary)
                snapshots.sort(key=lambda s: s["created_at"])
                self.transition = {"phase": "stopping", "deadline": None,
                                   "prepared_until": self.clock() + 30,
                                   "servers": [{**s, "original": copy.deepcopy(s), "live": s,
                                                "status": "running", "attempts": 0, "error": None}
                                               for s in snapshots]}
            self.transition.update(reason="gaming", phase="stopping", deadline=None, prepared_until=self.clock() + 30)
            self._save()  # Durable restoration intent precedes the first signal.
            errors = []
            for entry in self.transition["servers"]:
                try:
                    # Also catches a process spawned just before a backend crash
                    # persisted its new identity into the transition record.
                    if entry["id"] == "primary":
                        self.manager.stop()
                    else:
                        live = self.auxiliary.snapshot(settings["auxiliary"]) if settings["auxiliary"] else entry.get("live")
                        if live:
                            self.auxiliary.stop(live)
                    entry["live"] = None
                    if entry["status"] != "cancelled":
                        entry.update(status="stopped", attempts=0, error=None)
                except Exception:
                    entry.update(status="stop_failed", error="Process shutdown could not be confirmed.")
                    errors.append(entry["name"])
                self._save()
            if errors:
                self.transition["phase"] = "failed"
                self._save()
                raise LaunchError("Cannot release AI memory: " + ", ".join(errors))
            self.transition["phase"] = "gaming"
            self.transition["prepared_until"] = self.clock() + 30
            self._save()
            return self.state()

    def session_ended(self):
        # The hook is a hint, not proof that all clients have disconnected.
        # Never poll Apollo synchronously from its own blocking undo hook.
        self.wake_event.set()
        return {"scheduled": True}

    def retry(self):
        with self.lock:
            if self.connected is None or self.connected or not self.reconciled:
                raise LaunchError("Confirm that all Moonlight clients are disconnected before retrying.")
            if not self.transition or self.transition["phase"] != "failed":
                raise LaunchError("No failed restoration to retry.")
            if any(s["status"] == "stop_failed" for s in self.transition["servers"]):
                raise LaunchError("Resolve the process shutdown failure before retrying restoration.")
            for entry in self.transition["servers"]:
                if entry["status"] == "failed":
                    entry.update(attempts=0, status="stopped", error=None)
            self.transition.update(phase="countdown", deadline=self.clock())
            self._save()
            return self.state()

    def tick(self):
        with self.lock:
            settings = store.load_state()["gaming"]
            epoch = self.generation
        if not settings["enabled"]:
            with self.lock:
                if not self.transition or self.transition.get("reason") != "startup":
                    return
            clients = []
        else:
            try:
                clients = self.apollo.connected(settings)
            except Exception as exc:
                with self.lock:
                    self.connected = None
                    self.reconciled = False
                    self.integration_error = str(exc) if isinstance(exc, ValueError) else "Cannot reach Apollo; AI restoration is on hold."
                return
        with self.lock:
            if epoch != self.generation:
                return  # An API result from before preparation is stale.
            self.connected = clients
            self.reconciled = True
            self.integration_error = None
            if clients:
                if not self.transition or self.transition["phase"] not in ("gaming", "stopping"):
                    self.prepare()
                if self.transition:
                    self.transition.update(phase="gaming", deadline=None, prepared_until=None)
                    self._save()
                return
            if not self.transition:
                return
            t = self.transition
            now = self.clock()
            # Recover a crash in the middle of stopping before allowing loads.
            if t["phase"] == "stopping":
                self.prepare()
                return
            if t["phase"] == "gaming":
                if t.get("prepared_until") and now < t["prepared_until"]:
                    return
                t.update(phase="countdown", deadline=now + 60, prepared_until=None)
                self._save()
                return
            if t["phase"] == "failed":
                if all(s["status"] == "cancelled" for s in t["servers"]):
                    self.transition = None
                    self._save()
                return
            if t["phase"] in ("countdown", "retry_countdown"):
                if now < t["deadline"]:
                    return
                t.update(phase="restoring", deadline=None)
                self._save()
            self._restore_step(now)

    def _restore_step(self, now):
        entries = self.transition["servers"]
        for entry in entries:
            if entry["status"] in ("ready", "cancelled", "failed", "stop_failed"):
                continue
            if entry["status"] in ("running", "stopped"):
                entry["attempts"] += 1
                entry.update(status="loading", load_deadline=now + 300)
                self._save()  # Recovery can find a spawned process by registration.
                try:
                    if entry["id"] == "primary":
                        live = self.manager.snapshot()
                        if live and live["argv"] != entry["original"]["argv"]:
                            entry.update(status="stop_failed", error="Managed invocation changed; restoration is blocked.")
                            self._save()
                            continue
                        entry["live"] = live or self.manager.restore(entry["original"])
                    else:
                        registration = store.load_state()["gaming"]["auxiliary"]
                        live = self.auxiliary.snapshot(registration)
                        if live and live["argv"] != entry["original"]["argv"]:
                            entry.update(status="stop_failed", error="Embedding invocation changed; restoration is blocked.")
                            self._save()
                            continue
                        entry["live"] = live or self.auxiliary.restore(entry["original"])
                except Exception:
                    self._load_failed(entry, "Server could not be launched with its captured settings.")
                self._save()
            if entry["status"] == "loading":
                # A backend crash may leave status=loading but no saved live PID.
                if not entry.get("live"):
                    live = (self.manager.snapshot() if entry["id"] == "primary" else
                            self.auxiliary.snapshot(store.load_state()["gaming"]["auxiliary"]))
                    if live and live["argv"] == entry["original"]["argv"]:
                        entry["live"] = live
                live = entry.get("live")
                if not live or not identity_matches(live):
                    self._load_failed(entry, "Server exited before becoming ready.")
                elif (self.health(entry["port"]) if self.health else self._health(entry["port"], live["pid"])):
                    entry.update(status="ready", error=None)
                elif now >= entry["load_deadline"]:
                    self._load_failed(entry, "Server did not become ready within five minutes.")
                self._save()
            if entry["status"] == "loading":
                return  # Preserve startup order, without blocking connection checks.
        failures = [s for s in entries if s["status"] == "failed"]
        if any(s["status"] == "stop_failed" for s in entries):
            self.transition.update(phase="failed", deadline=None)
        elif failures and any(s["attempts"] < 2 for s in failures):
            for entry in failures:
                if entry["attempts"] < 2:
                    entry["status"] = "stopped"
            self.transition.update(phase="retry_countdown", deadline=max(s["retry_at"] for s in failures if s["attempts"] < 2))
        elif failures:
            self.transition.update(phase="failed", deadline=None)
        else:
            self.transition = None
        self._save()

    def _load_failed(self, entry, message):
        try:
            if entry["id"] == "primary":
                self.manager.stop()
            elif entry.get("live") or getattr(self.auxiliary, "proc", None):
                self.auxiliary.stop(entry.get("live"))
        except Exception:
            entry.update(status="stop_failed", error="Failed loading process could not be stopped.")
            return
        entry.update(status="failed", live=None, error=message, retry_at=self.clock() + 60)
