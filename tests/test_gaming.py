"""Connection and restoration races without touching real models or Apollo."""
import copy
import threading
from types import SimpleNamespace

import pytest

import gaming
import store
from launcher import LaunchError


class FakeServer:
    def __init__(self, server_id, port, live=True):
        self.record = {"id": server_id, "name": server_id, "port": port, "pid": port,
                       "created_at": float(port), "executable": "C:/llama-server.exe",
                       "argv": ["C:/llama-server.exe", "-m", server_id + ".gguf", "--port", str(port)],
                       "cwd": "C:/models", "config": {"name": "unsaved", "flags": [{"flag": "-c", "value": "12345"}]}}
        self.live = live
        self.loads = []
        self.stops = 0
        self.fail_load = False
        self.fail_stop = False

    def snapshot(self, *args):
        return copy.deepcopy(self.record) if self.live else None

    def stop(self, *args):
        self.stops += 1
        if self.fail_stop:
            raise LaunchError("access denied")
        self.live = False

    def restore(self, snapshot):
        self.loads.append(copy.deepcopy(snapshot))
        if self.fail_load:
            raise LaunchError("out of memory")
        self.live = True
        self.record = {**copy.deepcopy(snapshot), "pid": snapshot["pid"] + len(self.loads)}
        return self.snapshot()

    def launch(self, config):
        self.live = True
        return config

    def restart(self):
        self.live = True


class FakeApollo:
    clients = []
    error = None

    def connected(self, settings):
        if self.error:
            raise self.error
        return list(self.clients)


@pytest.fixture
def rig(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "HOME_DIR", str(tmp_path))
    monkeypatch.setattr(store, "STATE_PATH", str(tmp_path / "state.json"))
    store.update_gaming(enabled=True, auxiliary={"executable": "C:/llama-server.exe", "port": 8081})
    primary = FakeServer("primary", 8001)
    auxiliary = FakeServer("embeddings", 8081)
    apollo = FakeApollo()
    now = [10000.0]
    ready = {8001: True, 8081: True}
    monkeypatch.setattr(gaming, "identity_matches", lambda record: any(
        s.live and s.record["pid"] == record["pid"] for s in (primary, auxiliary)))
    def create():
        return gaming.GamingCoordinator(primary, auxiliary=auxiliary, apollo=apollo,
                                        clock=lambda: now[0], health=lambda port: ready[port])
    return create(), primary, auxiliary, apollo, now, ready, create


def disconnect(rig):
    c, primary, auxiliary, apollo, now, *_ = rig
    c.prepare()
    apollo.clients = ["a"]
    c.tick()
    apollo.clients = []
    c.tick()
    now[0] += 60


@pytest.mark.parametrize("running", [(True, True), (True, False), (False, True), (False, False)])
def test_restore_only_running_servers_and_exact_unsaved_settings(rig, running):
    c, primary, auxiliary, _, _, _, _ = rig
    primary.live, auxiliary.live = running
    disconnect(rig)
    store.upsert_config({"name": "unsaved", "flags": [{"flag": "-c", "value": "1"}]})
    c.tick()
    assert (primary.live, auxiliary.live) == running
    assert c.state()["phase"] == "normal"
    for server, was_running in zip((primary, auxiliary), running):
        assert len(server.loads) == int(was_running)
        if was_running:
            assert server.loads[0]["config"]["flags"][0]["value"] == "12345"
            assert server.loads[0]["cwd"] == "C:/models"


def test_multiple_clients_and_repeated_hooks_do_not_reload(rig):
    c, p, a, apollo, now, *_ = rig
    c.prepare()
    original = copy.deepcopy(c.transition["servers"][0]["original"])
    apollo.clients = ["a", "b"]
    c.tick()
    c.prepare()
    apollo.clients = ["b"]
    now[0] += 120
    c.session_ended()
    c.tick()
    assert c.state()["phase"] == "gaming"
    assert not p.loads and not a.loads
    assert c.transition["servers"][0]["original"] == original


def test_failed_stream_launch_gets_prepare_reservation_then_cooldown(rig):
    c, p, a, _, now, *_ = rig
    c.prepare()
    now[0] += 29
    c.tick()
    assert c.state()["phase"] == "gaming"
    now[0] += 1
    c.tick()
    assert c.state()["countdown"] == 60
    now[0] += 59
    c.tick()
    assert not p.loads
    now[0] += 1
    c.tick()
    assert p.live and a.live


def test_brief_reconnect_cancels_countdown(rig):
    c, p, a, apollo, now, *_ = rig
    disconnect(rig)
    now[0] -= 1
    c.prepare()
    apollo.clients = ["a"]
    c.tick()
    now[0] += 90
    c.tick()
    assert not p.loads and not a.loads


def test_stop_failure_retains_intent_and_blocks_preparation(rig):
    c, p, a, _, _, _, _ = rig
    p.fail_stop = True
    with pytest.raises(LaunchError, match="Cannot release"):
        c.prepare()
    assert p.live and not a.live
    saved = store.load_state()["gaming"]["transition"]
    assert saved["phase"] == "failed"
    assert saved["servers"][0]["status"] == "stop_failed"


def test_two_load_failures_then_no_more_automatic_attempts(rig):
    c, p, a, _, now, *_ = rig
    p.fail_load = True
    disconnect(rig)
    c.tick()
    assert c.state()["phase"] == "retry_countdown"
    assert a.live and len(a.loads) == 1
    now[0] += 59
    c.tick()
    assert len(p.loads) == 1
    now[0] += 1
    c.tick()
    assert c.state()["phase"] == "failed"
    for _ in range(3):
        now[0] += 600
        c.tick()
    assert len(p.loads) == 2 and len(a.loads) == 1
    p.fail_load = False
    c.retry()
    c.tick()
    assert c.state()["phase"] == "normal"
    assert len(p.loads) == 3 and len(a.loads) == 1


def test_first_load_failure_second_success(rig):
    c, p, a, _, now, *_ = rig
    p.fail_load = True
    disconnect(rig)
    c.tick()
    p.fail_load = False
    now[0] += 60
    c.tick()
    assert p.live and a.live and c.state()["phase"] == "normal"


def test_new_session_after_terminal_failure_does_not_retry_stopped_model(rig):
    c, p, a, apollo, now, *_ = rig
    p.fail_load = True
    disconnect(rig)
    c.tick()
    now[0] += 60
    c.tick()
    assert c.state()["phase"] == "failed"
    c.prepare()
    apollo.clients = ["new"]
    c.tick()
    apollo.clients = []
    c.tick()
    now[0] += 60
    c.tick()
    assert len(p.loads) == 2 and a.live


def test_prepare_discovers_missed_connection(rig):
    c, p, a, apollo, *_ = rig
    apollo.clients = ["missed-hook"]
    c.tick()
    assert c.state()["phase"] == "gaming" and not p.live and not a.live


def test_process_exit_before_health_is_failed_loading(rig):
    c, p, _, _, _, ready, _ = rig
    ready[8001] = False
    disconnect(rig)
    c.tick()
    p.live = False
    c.tick()
    assert c.state()["phase"] == "retry_countdown"


def test_unknown_state_blocks_manual_retry(rig):
    c, p, _, apollo, now, *_ = rig
    p.fail_load = True
    disconnect(rig)
    c.tick()
    now[0] += 60
    c.tick()
    apollo.error = ValueError("offline")
    c.tick()
    with pytest.raises(LaunchError, match="Confirm"):
        c.retry()


def test_registered_embedding_port_cannot_be_used_by_primary(rig):
    c, *_ = rig
    with pytest.raises(LaunchError, match="registered for embeddings"):
        c.launch({"port": 8081})


def test_health_checks_port_ownership_before_http(monkeypatch):
    calls = []
    class Process:
        def net_connections(self, kind):
            return []
    monkeypatch.setattr(gaming.psutil, "Process", lambda pid: Process())
    monkeypatch.setattr(gaming.httpx, "Client", lambda **kwargs: calls.append(True))
    assert not gaming.GamingCoordinator._health(8001, 10)
    assert not calls


@pytest.mark.parametrize("code,status,expected", [(503, "loading", False), (200, "ok", True), (200, "loading", False)])
def test_health_requires_ready_response(monkeypatch, code, status, expected):
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url):
            assert url == "http://127.0.0.1:8001/health"
            return SimpleNamespace(status_code=code, json=lambda: {"status": status})
    monkeypatch.setattr(gaming.httpx, "Client", lambda **kwargs: Client())
    assert gaming.GamingCoordinator._health(8001) == expected


def test_loading_requires_health_and_can_be_preempted(rig):
    c, p, a, apollo, _, ready, _ = rig
    ready[8001] = False
    disconnect(rig)
    c.tick()
    assert c.state()["phase"] == "restoring"
    assert p.live and not a.live
    snapshot = copy.deepcopy(c.transition["servers"][0]["original"])
    c.prepare()
    apollo.clients = ["a"]
    c.tick()
    assert not p.live and c.state()["phase"] == "gaming"
    assert c.transition["servers"][0]["original"] == snapshot


def test_load_timeout_stops_process_before_retry(rig):
    c, p, _, _, now, ready, _ = rig
    ready[8001] = False
    disconnect(rig)
    c.tick()
    now[0] += 300
    c.tick()
    assert not p.live
    assert c.state()["phase"] == "retry_countdown"


def test_unknown_apollo_state_holds_restoration(rig):
    c, p, a, apollo, now, *_ = rig
    disconnect(rig)
    apollo.error = ValueError("HTTP 401")
    now[0] += 1000
    c.tick()
    assert not p.live and not a.live
    assert c.state()["connected_clients"] is None
    assert c.state()["integration_error"]
    apollo.error = None
    c.tick()
    assert p.live and a.live


@pytest.mark.parametrize("phase", ["gaming", "countdown", "restoring", "retry_countdown", "failed"])
def test_backend_restart_recovers_persisted_transition(rig, phase):
    c, p, _, apollo, now, ready, create = rig
    disconnect(rig)
    if phase == "gaming":
        apollo.clients = ["a"]
        c.prepare()
    elif phase == "countdown":
        now[0] -= 30
    elif phase in ("restoring", "retry_countdown", "failed"):
        if phase == "restoring":
            ready[8001] = False
        else:
            p.fail_load = True
        c.tick()
        if phase == "failed":
            now[0] += 60
            c.tick()
    restarted = create()
    assert restarted.state()["phase"] == phase
    apollo.error = ValueError("offline")
    restarted.tick()
    assert restarted.state()["integration_error"]
    apollo.error = None
    restarted.tick()
    assert restarted.state()["phase"] == phase


def test_explicit_stop_cancels_primary_restore_only(rig):
    c, p, a, _, _, _, _ = rig
    disconnect(rig)
    c.stop_server()
    c.tick()
    assert not p.live and not p.loads and a.live


def test_lifecycle_actions_blocked_and_cancel_all_releases_failed_state(rig):
    c, p, _, _, now, *_ = rig
    p.fail_load = True
    disconnect(rig)
    with pytest.raises(LaunchError, match="blocked"):
        c.launch({})
    c.tick()
    now[0] += 60
    c.tick()
    c.cancel()
    assert c.state()["phase"] == "normal"


def test_snapshot_persisted_before_any_stop(rig, monkeypatch):
    c, p, *_ = rig
    original_stop = p.stop
    def stop():
        assert store.load_state()["gaming"]["transition"]["servers"][0]["original"]["argv"]
        original_stop()
    monkeypatch.setattr(p, "stop", stop)
    c.prepare()


def test_backend_recovers_crash_during_shutdown(rig):
    c, p, a, _, _, _, create = rig
    c.prepare()
    c.transition["phase"] = "stopping"
    c._save()
    p.live = True
    restored = create()
    restored.tick()
    assert not p.live and not a.live
    assert restored.state()["phase"] == "gaming"


def test_stale_connection_result_cannot_override_new_prepare(rig):
    c, _, _, apollo, _, _, _ = rig
    started = threading.Event()
    release = threading.Event()
    def delayed(settings):
        started.set()
        release.wait(2)
        return []
    apollo.connected = delayed
    worker = threading.Thread(target=c.tick)
    worker.start()
    assert started.wait(2)
    c.prepare()
    release.set()
    worker.join(2)
    assert c.state()["phase"] == "gaming"
    assert c.state()["connected_clients"] is None


def test_concurrent_configuration_writes_preserve_gaming_transition(rig):
    c, *_ = rig
    c.prepare()
    threads = [threading.Thread(target=store.upsert_config, args=({"name": str(i)},)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(store.list_configs()) == 20
    assert store.load_state()["gaming"]["transition"]["phase"] == "gaming"


def enable_boot(rig, marker=1):
    c, p, a, *_ = rig
    snapshots = c.capture_startup_models()
    p.live = a.live = False
    store.update_startup(installed=True, autostart_models=True, models=snapshots, boot_marker=marker)
    return snapshots


def test_boot_loads_captured_models_and_is_once_per_boot(rig):
    c, p, a, *_ = rig
    snapshots = enable_boot(rig)
    c.schedule_boot_models(2)
    assert c.state()["reason"] == "startup"
    c.tick()
    assert p.live and a.live
    assert p.loads[0] == snapshots[0] and a.loads[0] == snapshots[1]
    c.stop_server()
    c.schedule_boot_models(2)
    c.tick()
    assert not p.live and len(p.loads) == 1
    c.schedule_boot_models(3)
    c.tick()
    assert p.live and len(p.loads) == 2 and len(a.loads) == 1


def test_boot_models_work_with_gaming_disabled(rig):
    c, p, a, apollo, *_ = rig
    enable_boot(rig)
    store.update_gaming(enabled=False)
    apollo.error = ValueError("must not contact Apollo")
    c.schedule_boot_models(2)
    c.tick()
    assert p.live and a.live and c.state()["phase"] == "normal"


def test_boot_model_loading_waits_for_unknown_apollo_state(rig):
    c, p, a, apollo, *_ = rig
    enable_boot(rig)
    apollo.error = ValueError("unknown")
    c.schedule_boot_models(2)
    c.tick()
    assert not p.live and not a.live
    apollo.error = None
    c.tick()
    assert p.live and a.live


def test_moonlight_preempts_boot_queue_and_models_resume_afterwards(rig):
    c, p, a, apollo, now, *_ = rig
    enable_boot(rig)
    c.schedule_boot_models(2)
    apollo.clients = ["gaming"]
    c.tick()
    assert not p.live and not a.live and c.state()["phase"] == "gaming"
    apollo.clients = []
    c.tick()
    now[0] += 60
    c.tick()
    assert p.live and a.live


def test_prior_gaming_recovery_takes_priority_over_boot_preset(rig):
    c, *_ = rig
    disconnect(rig)
    saved = copy.deepcopy(c.transition)
    store.update_startup(installed=True, autostart_models=True, models=[], boot_marker=1)
    c.schedule_boot_models(2)
    assert c.transition == saved
    assert store.load_state()["startup"]["boot_marker"] == 2


def test_boot_failure_keeps_two_attempt_limit(rig):
    c, p, a, _, now, *_ = rig
    enable_boot(rig)
    p.fail_load = True
    c.schedule_boot_models(2)
    c.tick()
    assert a.live and c.state()["phase"] == "retry_countdown"
    now[0] += 60
    c.tick()
    assert len(p.loads) == 2 and c.state()["phase"] == "failed"
    c.schedule_boot_models(2)
    c.tick()
    assert len(p.loads) == 2
