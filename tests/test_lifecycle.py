"""Ordering, cancellation and fallback without loading models or touching user servers."""
import copy
import socket
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import flags
import managed_logs
import store
from gaming import GamingCoordinator
from launcher import LaunchError, ServerManager
from lifecycle import ModelLifecycle


class Manager:
    def __init__(self, role, events):
        self.role, self.events = role, events
        self.current, self.run_id, self.live = None, None, False
        self.failed = False
        self.spawn_error = False
        self.stop_error = False
        self.attempts = []
        self._proc = None

    def validate(self, config, **kwargs):
        if config.get("invalid"):
            raise LaunchError("Invalid model file")
        return copy.deepcopy(config), "llama-server"

    def snapshot(self):
        return {"pid": 123 if self.role == "primary" else 456, "id": self.role,
                "config": self.current, "port": self.current["port"], "managed": True,
                "name": self.current["name"], "created_at": 2 if self.role == "primary" else 1,
                "executable": "llama-server", "cwd": "/models",
                "argv": ["llama-server", "-m", self.current["model_path"]]} if self.live else None

    def status(self):
        return {"state": "running" if self.live else "exited" if self.failed else "stopped",
                "config": self.current, "config_name": (self.current or {}).get("name")}

    def launch(self, config):
        self.events.append(("launch", self.role))
        self.attempts.append(copy.deepcopy(config))
        if self.spawn_error:
            raise LaunchError("Spawn failed")
        self.current = copy.deepcopy(config)
        self.live, self.failed = True, False
        self.run_id = f"{self.role}-{len(self.attempts)}"
        return self.status()

    def stop(self, reason=None):
        if self.stop_error:
            raise LaunchError("Identity unavailable")
        self.events.append(("stop", self.role))
        self.live = False

    def restore(self, snapshot):
        self.launch(snapshot["config"])
        return self.snapshot()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    for key, path in {"HOME_DIR": tmp_path, "STATE_PATH": tmp_path / "state.json",
                      "MANAGED_LOG": tmp_path / "primary.log"}.items():
        monkeypatch.setattr(store, key, str(path))
    monkeypatch.setattr(flags, "get_server_flags", lambda _: {"source": "bundled", "flags": []})
    monkeypatch.setattr("lifecycle.owns_port", lambda snapshot: True)
    events = []
    primary, embeddings = Manager("primary", events), Manager("embeddings", events)
    gaming = GamingCoordinator(primary)
    now = [1000.]
    lifecycle = ModelLifecycle(primary, embeddings, gaming, reserved_ports=(8500, 8501), clock=lambda: now[0])
    monkeypatch.setattr(lifecycle, "_check_port", lambda *args: None)
    readiness = {"primary": False, "embeddings": False}
    for role, manager in (("primary", primary), ("embeddings", embeddings)):
        lifecycle.verifiers[role] = SimpleNamespace(readiness=lambda role=role, manager=manager: {
            "ready": readiness[role] and manager.live,
            "state": "loading" if manager.live else "failed", "reason": "Model loading or exited."})
    return SimpleNamespace(c=lifecycle, p=primary, e=embeddings, g=gaming, ready=readiness, now=now, events=events)


P = {"name": "primary", "model_path": "/models/primary.gguf", "port": 8001, "flags": []}
E = {"name": "embedding", "model_path": "/models/embedding.gguf", "port": 8081, "flags": []}


def test_primary_readiness_gates_automatic_companion(rig):
    store.upsert_config(E, "embeddings")
    store.set_default_config(E["name"], "embeddings")
    store.update_settings(role="embeddings", autostart_after_primary=True)
    rig.c.launch_primary(P)
    rig.c.tick()
    assert not rig.e.live and rig.c.state()["state"] == "waiting"
    rig.ready["primary"] = True
    rig.c.tick()
    assert rig.e.live and rig.c.state()["state"] == "loading"
    rig.ready["embeddings"] = True
    rig.c.tick()
    assert rig.c.state()["ready"]


def test_standalone_embeddings_are_stopped_before_primary_then_restored(rig):
    rig.c.launch_embeddings(E)
    assert rig.e.live
    rig.events.clear()
    rig.c.launch_primary(P)
    assert rig.events == [("stop", "embeddings"), ("launch", "primary")]
    rig.c.tick()
    assert not rig.e.live
    rig.ready["primary"] = True
    rig.c.tick()
    assert rig.e.live


def test_invalid_primary_does_not_interrupt_embeddings(rig):
    rig.c.launch_embeddings(E)
    with pytest.raises(LaunchError):
        rig.c.launch_primary({**P, "invalid": True})
    assert rig.e.live and not rig.p.live


@pytest.mark.parametrize("stop_primary", [False, True])
def test_stop_cancels_pending_launch_without_resurrection(rig, stop_primary):
    rig.c.launch_primary(P)
    rig.c.launch_embeddings(E)
    op_id = rig.c.state()["operation_id"]
    (rig.c.stop_primary if stop_primary else rig.c.stop_embeddings)()
    rig.ready["primary"] = True
    rig.c.tick()
    assert not rig.e.live and rig.c.state()["state"] == "stopped"
    assert rig.c.state()["operation_id"] == op_id
    assert rig.p.live is not stop_primary


def test_failed_or_timed_out_primary_leaves_embeddings_waiting(rig):
    rig.c.launch_primary(P)
    rig.c.launch_embeddings(E)
    rig.now[0] += 301
    rig.c.tick()
    rig.ready["primary"] = True
    rig.c.tick()
    assert not rig.e.live and "timed out" in rig.c.state()["reason"]


def test_replacement_operation_invalidates_queued_configuration(rig):
    rig.c.launch_primary(P)
    rig.c.launch_embeddings(E)
    old_id = rig.c.state()["operation_id"]
    rig.c.launch_embeddings({**E, "name": "new"})
    assert rig.c.state()["operation_id"] != old_id
    rig.ready["primary"] = True
    rig.c.tick()
    assert [c["name"] for c in rig.e.attempts] == ["new"]


def test_cpu_retry_is_bounded_and_preserves_configuration(rig, monkeypatch):
    advertised = {"source": "help", "flags": [{"flags": [flag]} for flag in
                    ("--device", "--gpu-layers", "--no-op-offload")]}
    monkeypatch.setattr(flags, "get_server_flags", lambda _: advertised)
    config = {**E, "flags": [{"flag": "-ngl", "value": "99"}, {"flag": "-c", "value": "512"}]}
    original = copy.deepcopy(config)
    store.update_settings(role="embeddings", cpu_fallback=True)
    rig.c.launch_embeddings(config)
    op_id = rig.c.state()["operation_id"]
    rig.e.live, rig.e.failed = False, True
    rig.c.tick()
    assert rig.c.state()["state"] == "cpu_retry"
    rig.c.tick()
    effective = rig.e.attempts[-1]["flags"]
    assert {"flag": "--device", "value": "none"} in effective
    assert not any(f["flag"] == "-ngl" for f in effective)
    assert config == original and rig.c.state()["operation_id"] == op_id
    rig.e.live, rig.e.failed = False, True
    rig.c.tick()
    rig.c.tick()
    assert rig.c.state()["state"] == "failed" and len(rig.e.attempts) == 2


def test_cpu_retry_success_reports_mode_and_no_crash_restart(rig, monkeypatch):
    store.update_settings(role="embeddings", cpu_fallback=True)
    monkeypatch.setattr(rig.c, "cpu_config", lambda config: copy.deepcopy(config))
    rig.c.launch_embeddings(E)
    rig.now[0] += 301
    rig.c.tick()
    rig.c.tick()
    rig.ready["embeddings"] = True
    rig.c.tick()
    assert rig.c.state()["mode"] == "cpu" and rig.c.state()["ready"]
    rig.e.live = False
    rig.c.tick()
    rig.c.tick()
    assert rig.c.state()["state"] == "failed" and len(rig.e.attempts) == 2


@pytest.mark.parametrize("failure", ["cleanup", "unsupported"])
def test_fallback_never_spawns_without_cleanup_and_supported_binary(rig, failure):
    store.update_settings(role="embeddings", cpu_fallback=True)
    rig.c.launch_embeddings(E)
    rig.e.live = False
    if failure == "cleanup":
        rig.e.stop_error = True
    rig.c.tick()
    rig.c.tick()
    assert rig.c.state()["state"] == "failed" and len(rig.e.attempts) == 1


def test_port_conflicts_do_not_trigger_fallback(rig):
    store.update_settings(role="embeddings", cpu_fallback=True)
    with pytest.raises(LaunchError, match="dashboard"):
        rig.c.launch_embeddings({**E, "port": 8500})
    store.update_gaming(auxiliary={"port": 8081, "executable": "llama-server"})
    with pytest.raises(LaunchError, match="legacy"):
        rig.c.launch_embeddings(E)
    assert not rig.e.attempts


def test_real_listener_is_rejected():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with pytest.raises(LaunchError, match="already in use"):
            ModelLifecycle._check_port(listener.getsockname()[1])


def test_close_and_gaming_cancel_queued_work(rig):
    rig.c.launch_primary(P)
    rig.c.launch_embeddings(E)
    rig.c.suspend()
    rig.ready["primary"] = True
    rig.c.tick()
    assert not rig.e.live
    rig.c.launch_embeddings(E)
    rig.c.close()
    with pytest.raises(LaunchError, match="shutting down"):
        rig.c.launch_embeddings(E)


def test_role_configs_records_and_logs_are_independent(rig):
    store.upsert_config({**P, "name": "same"})
    store.upsert_config({**E, "name": "same"}, "embeddings")
    store.set_default_config("same", "embeddings")
    store.set_running({"pid": 1})
    store.set_running({"pid": 2}, "embeddings")
    store.delete_config("same")
    assert store.get_config("same", "embeddings")["port"] == 8081
    assert store.get_running()["pid"] == 1 and store.get_running("embeddings")["pid"] == 2
    primary_id, embedding_id = "a" * 32, "b" * 32
    managed_logs.Writer(primary_id)
    managed_logs.Writer(embedding_id, role="embeddings")
    assert managed_logs.latest() == primary_id
    assert managed_logs.latest("embeddings") == embedding_id


def test_embedding_argv_and_separate_record(rig):
    manager = ServerManager(lambda *args: pytest.fail("must not retarget"), role="embeddings")
    argv = manager.build_argv(E, "llama-server")
    assert "--embedding" in argv and argv[argv.index("--port") + 1] == "8081"
    manager._set_record({"pid": 9})
    assert store.get_running() is None and store.get_running("embeddings")["pid"] == 9


def test_managed_gaming_recovery_gates_embedding_and_cpu_retry(rig, monkeypatch):
    monkeypatch.setattr("gaming.identity_matches", lambda snapshot: True)
    monkeypatch.setattr(rig.c, "cpu_config", lambda config: copy.deepcopy(config))
    rig.g.clock = lambda: rig.now[0]
    rig.g.apollo = SimpleNamespace(connected=lambda settings: [])
    rig.g.health = lambda port: rig.ready["primary" if port == 8001 else "embeddings"]
    store.update_gaming(enabled=True)
    store.update_settings(role="embeddings", cpu_fallback=True)
    rig.c.launch_embeddings(E)
    rig.c.launch_primary(P)
    rig.ready["primary"] = True
    rig.c.tick()
    rig.ready["embeddings"] = True
    rig.c.tick()
    rig.g.prepare()
    rig.g.transition["prepared_until"] = None
    assert not rig.p.live and not rig.e.live
    assert [s["id"] for s in rig.g.transition["servers"]] == ["primary", "embeddings"]
    rig.ready["primary"] = False
    rig.g.tick()
    rig.now[0] += 60
    rig.g.tick()
    assert rig.p.live and not rig.e.live
    rig.ready["primary"] = True
    rig.ready["embeddings"] = False
    rig.g.tick()
    assert rig.e.live
    rig.e.live = False
    rig.c.tick()
    rig.g.tick()
    assert rig.c.state()["state"] == "cpu_retry"
    rig.c.tick()
    rig.ready["embeddings"] = True
    rig.c.tick()
    rig.g.tick()
    assert rig.g.transition is None and rig.c.state()["mode"] == "cpu"


def test_boot_stops_existing_embeddings_before_missing_primary(rig):
    rig.c.launch_embeddings(E)
    store.update_startup(installed=True, autostart_models=True, models=[{
        "id": "primary", "name": "primary", "port": 8001, "created_at": 2, "config": P}], boot_marker=1)
    rig.g.schedule_boot_models(2)
    assert not rig.e.live
    assert [s["id"] for s in rig.g.transition["servers"]] == ["primary", "embeddings"]


def test_pending_port_conflict_never_qualifies_for_cpu_fallback(rig, monkeypatch):
    store.update_settings(role="embeddings", cpu_fallback=True)
    rig.c.launch_primary(P)
    rig.c.launch_embeddings(E)
    monkeypatch.setattr(rig.c, "_check_port", Mock(side_effect=LaunchError("port busy")))
    rig.ready["primary"] = True
    rig.c.tick()
    assert rig.c.state()["state"] == "failed" and not rig.e.attempts


@pytest.mark.parametrize("state", ["waiting", "cpu_retry", "stopped"])
def test_native_readoption_reconciles_interrupted_operations(rig, state):
    rig.c.launch_embeddings(E)
    operation_id = rig.c.state()["operation_id"]
    store.set_running({"pid": 456, "run_id": rig.e.run_id, "config": E}, "embeddings")
    store.update_state(lambda data: data["embeddings"]["operation"].update(state=state, run_id=None))
    second_gaming = GamingCoordinator(rig.p)
    resumed = ModelLifecycle(rig.p, rig.e, second_gaming)
    assert resumed.state()["operation_id"] == operation_id
    assert len(rig.e.attempts) == 1
    if state == "stopped":
        assert not rig.e.live and resumed.state()["state"] == "stopped"
    else:
        assert rig.e.live and resumed.state()["state"] == "loading"
        assert resumed.state()["run_id"] == rig.e.run_id


def test_stop_failure_stays_visible_and_does_not_launch_replacement(rig):
    rig.c.launch_embeddings(E)
    rig.e.stop_error = True
    with pytest.raises(LaunchError):
        rig.c.launch_primary(P)
    assert rig.e.live and not rig.p.live
    assert rig.c.state()["state"] == "failed" and "shutdown" in rig.c.state()["reason"]


def test_failed_stop_still_cancels_boot_recovery_intent(rig):
    rig.c.launch_embeddings(E)
    rig.e.stop_error = True
    rig.g.transition = {"phase": "restoring", "reason": "startup", "servers": [
        {"id": role, "managed": True, "status": "loading"} for role in ("primary", "embeddings")]}
    with pytest.raises(LaunchError):
        rig.g.stop_server()
    assert all(s["status"] == "cancelled" for s in rig.g.transition["servers"])
    rig.g.tick()
    assert rig.g.transition is None and len(rig.e.attempts) == 1


def test_primary_process_discovery_never_sums_embedding_listener(monkeypatch):
    import collectors
    monkeypatch.setattr(collectors.psutil, "net_connections", lambda **kwargs: [
        SimpleNamespace(status="LISTEN", laddr=SimpleNamespace(port=8001), pid=123),
        SimpleNamespace(status="LISTEN", laddr=SimpleNamespace(port=8081), pid=456)])
    monkeypatch.setattr(collectors.psutil, "process_iter", lambda *args: pytest.fail("do not discover by name"))
    assert collectors.find_llama_pids(port=8001) == {123}
