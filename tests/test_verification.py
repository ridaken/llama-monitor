import argparse
import asyncio
from types import SimpleNamespace

import httpx
import psutil
import pytest
from fastapi.testclient import TestClient

import app as app_module
import store
import verification as v

HTTP_CLIENT = httpx.Client


@pytest.fixture
def probe(monkeypatch, tmp_path):
    for key, value in {"HOME_DIR": tmp_path, "STATE_PATH": tmp_path / "state.json",
                       "MANAGED_LOG": tmp_path / "server.log", "PROMPTS_DIR": tmp_path / "prompts"}.items():
        monkeypatch.setattr(store, key, str(value))
    snapshot = {"pid": 123, "created_at": 1, "executable": "server", "port": 8001}
    store.set_running({"pid": 123, "run_id": "test-run"})
    manager = SimpleNamespace(status=lambda: {"state": "running", "pid": 123}, snapshot=lambda: snapshot.copy())
    gaming = SimpleNamespace(state=lambda: {"blocked": False})
    instance = v.ManagedVerification(manager, gaming)
    monkeypatch.setattr(v, "owns_port", lambda _: True)
    real_client = httpx.Client
    monkeypatch.setattr(v.httpx, "Client", lambda **kw: real_client(
        **kw, transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"status": "ok"}))))
    return instance


def inference(monkeypatch, handler):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(v.httpx, "AsyncClient", lambda **kw: real_client(**kw, transport=httpx.MockTransport(handler)))


def test_readiness_requires_live_health_not_cache(probe, monkeypatch):
    assert probe.readiness()["state"] == "ready"
    monkeypatch.setattr(v.httpx, "Client", lambda **kw: HTTP_CLIENT(
        **kw, transport=httpx.MockTransport(lambda _: httpx.Response(503, json={"status": "loading model"}))))
    assert probe.readiness()["state"] == "loading"


def test_wrong_listener_is_never_probed(probe, monkeypatch):
    monkeypatch.setattr(v, "owns_port", lambda _: False)
    monkeypatch.setattr(v.httpx, "Client", lambda **kw: pytest.fail("must not contact another process"))
    assert probe.readiness()["ready"] is False


def test_port_ownership_checks_process_identity(monkeypatch):
    monkeypatch.setattr(v, "identity_matches", lambda _: True)
    monkeypatch.setattr(v.psutil, "Process", lambda _: SimpleNamespace(net_connections=lambda **kw: [
        SimpleNamespace(status=psutil.CONN_LISTEN, laddr=SimpleNamespace(port=9000))]))
    assert not v.owns_port({"pid": 1, "port": 8001})
    assert v.owns_port({"pid": 1, "port": 9000})
    monkeypatch.setattr(v, "identity_matches", lambda _: False)
    assert not v.owns_port({"pid": 1, "port": 9000})


@pytest.mark.parametrize("state,expected", [("stopped", "stopped"), ("exited", "failed")])
def test_stopped_and_early_exit(probe, state, expected):
    probe.manager.status = lambda: {"state": state, "exit_code": 9}
    assert probe.readiness()["state"] == expected


def test_gaming_block_prevents_probes(probe):
    probe.gaming.state = lambda: {"blocked": True}
    assert probe.readiness()["state"] == "blocked"
    with pytest.raises(v.VerificationError, match="pending"):
        asyncio.run(probe.smoke(1))


def test_identity_change_during_health_fails(probe):
    original = probe.manager.snapshot
    calls = []

    def snapshot():
        calls.append(1)
        return {**original(), "created_at": len(calls)}

    probe.manager.snapshot = snapshot
    assert probe.readiness()["state"] == "failed"


@pytest.mark.parametrize("offset", [-1, 1])
def test_linux_clock_shift_during_readiness_and_inference_is_allowed(probe, monkeypatch, offset):
    original = probe.manager.snapshot
    calls = []

    def snapshot():
        calls.append(1)
        return {**original(), "boot_id": "test-boot", "start_ticks": 3709,
                "created_at": 1000 + offset * len(calls)}

    probe.manager.snapshot = snapshot
    assert probe.readiness()["ready"]
    inference(monkeypatch, lambda _: httpx.Response(200, json={
        "choices": [{"message": {"content": "OK"}}]}))
    assert asyncio.run(probe.smoke(2))["message"]["content"] == "OK"


@pytest.mark.parametrize("change", [{"boot_id": "other-boot"}, {"start_ticks": 3710},
                                    {"pid": 124}, {"executable": "other-server"}])
def test_linux_identity_change_during_health_fails(probe, change):
    original = probe.manager.snapshot
    calls = []

    def snapshot():
        current = {**original(), "boot_id": "test-boot", "start_ticks": 3709}
        if calls:
            current.update(change)
        calls.append(1)
        return current

    probe.manager.snapshot = snapshot
    assert probe.readiness()["state"] == "failed"


def test_same_linux_process_with_changed_run_id_fails_verification(probe):
    original = probe.manager.snapshot
    probe.manager.snapshot = lambda: {**original(), "boot_id": "test-boot", "start_ticks": 3709}
    snapshot = probe._snapshot()
    store.set_running({"pid": 123, "run_id": "different-run"})
    with pytest.raises(v.VerificationError, match="changed during verification"):
        probe._unchanged(snapshot)


def test_process_access_error_is_reported_as_verification_failure(probe):
    def denied():
        raise psutil.AccessDenied(123)
    probe.manager.status = denied
    assert probe.readiness()["state"] == "failed"


def test_process_exit_during_inference_is_a_controlled_failure(probe, monkeypatch):
    def handler(_):
        def gone():
            raise psutil.NoSuchProcess(123)
        probe.manager.snapshot = gone
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
    inference(monkeypatch, handler)
    with pytest.raises(v.VerificationError, match="process identity"):
        asyncio.run(probe.smoke(2))


@pytest.mark.parametrize("message", [{"content": "OK"}, {"content": "", "reasoning_content": "thinking"}])
def test_smoke_performs_bounded_inference(probe, monkeypatch, message):
    calls = []

    def handler(request):
        import json
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "length"}],
                                       "usage": {"completion_tokens": 2}})

    inference(monkeypatch, handler)
    result = asyncio.run(probe.smoke(2))
    assert result["run_id"] == "test-run" and result["message"] == message
    assert result["elapsed_seconds"] >= 0 and calls[0]["max_tokens"] == 16 and calls[0]["stream"] is False


@pytest.mark.parametrize("body", [{}, {"choices": []}, {"choices": [{"message": {"content": ""}}]}, []])
def test_malformed_completions_fail(probe, monkeypatch, body):
    inference(monkeypatch, lambda _: httpx.Response(200, json=body))
    with pytest.raises(v.VerificationError, match="no generated content"):
        asyncio.run(probe.smoke(2))


def test_replacement_during_inference_fails(probe, monkeypatch):
    def handler(_):
        store.set_running({"pid": 123, "run_id": "replacement"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    inference(monkeypatch, handler)
    with pytest.raises(v.VerificationError, match="changed"):
        asyncio.run(probe.smoke(2))


def test_smoke_deadline_cancels_slow_response(probe, monkeypatch):
    async def handler(_):
        await asyncio.sleep(1)
        pytest.fail("request should have been cancelled")

    monkeypatch.setattr(probe, "readiness", lambda: {"ready": True, "run_id": "test-run"})
    inference(monkeypatch, handler)
    with pytest.raises(v.VerificationTimeout):
        asyncio.run(probe.smoke(0.02))


def test_api_contracts_and_validation(probe, monkeypatch):
    monkeypatch.setattr(app_module, "ManagedVerification", lambda *args: probe)
    monkeypatch.setattr(app_module.GamingCoordinator, "start", lambda _, **kwargs: None)
    args = argparse.Namespace(llama_url="http://127.0.0.1:9", llama_log=None, port=8500)
    inference(monkeypatch, lambda _: httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]}))
    # Use Starlette's client without replacing httpx.Client in the transport fixture.
    monkeypatch.setattr(v.httpx, "Client", HTTP_CLIENT)
    monkeypatch.setattr(probe, "readiness", lambda: {"ready": True, "state": "ready", "run_id": "test-run"})
    with TestClient(app_module.build_app(args)) as client:
        assert client.get("/api/launcher/readiness").json()["ready"]
        assert client.post("/api/launcher/smoke", json={"timeout": 1}).status_code == 200
        for timeout in (0, -1, 301):
            assert client.post("/api/launcher/smoke", json={"timeout": timeout}).status_code == 422
        async def timeout(_):
            raise v.VerificationTimeout("deadline")
        monkeypatch.setattr(probe, "smoke", timeout)
        assert client.post("/api/launcher/smoke", json={}).status_code == 504
        async def failed(_):
            raise v.VerificationError("model unavailable")
        monkeypatch.setattr(probe, "smoke", failed)
        assert client.post("/api/launcher/smoke", json={}).status_code == 409


@pytest.fixture
def prompt_test(probe):
    target = {"url": "http://127.0.0.1:8001", "managed": True, "run_id": "test-run"}

    async def state():
        return {"enabled": False, "listening": False, "host": "0.0.0.0", "port": 8501}

    async def validate(_):
        return True

    proxy = SimpleNamespace(target=lambda: target.copy(), state=state, validate_target=validate)
    return v.InferenceTest(probe, proxy)


def test_fixed_prompt_uses_active_target_and_preserves_smoke(probe, prompt_test, monkeypatch):
    import json
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "print('FizzBuzz')"}}]})

    inference(monkeypatch, handler)
    result = asyncio.run(prompt_test.run(2))
    assert result["route"] == "direct" and result["request_id"] is None
    assert result["run_id"] == "test-run" and result["prompt"] == v.TEST_PROMPT
    body = json.loads(calls[0].content)
    assert len(calls) == 1 and calls[0].url.port == 8001
    assert body == {"messages": [{"role": "user", "content": v.TEST_PROMPT}],
                    "temperature": 0, "max_tokens": 2048, "stream": False}
    assert asyncio.run(probe.smoke(2))["message"]["content"]
    assert json.loads(calls[1].content)["max_tokens"] == 16


@pytest.mark.parametrize("host,destination", [("0.0.0.0", "127.0.0.1"), ("::", "::1"), ("127.0.0.1", "127.0.0.1")])
def test_prompt_connects_to_real_proxy_listener(prompt_test, monkeypatch, host, destination):
    async def state():
        return {"enabled": True, "listening": True, "host": host, "port": 8501}

    prompt_test.proxy.state = state
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, headers={"X-Llama-Monitor-Request-ID": "request-one"},
                              json={"choices": [{"message": {"content": "answer"}}]})

    inference(monkeypatch, handler)
    result = asyncio.run(prompt_test.run(2))
    assert result["route"] == "proxy" and result["request_id"] == "request-one"
    assert len(calls) == 1 and calls[0].url.host == destination and calls[0].url.port == 8501


def test_prompt_does_not_bypass_unavailable_proxy(prompt_test, monkeypatch):
    async def state():
        return {"enabled": True, "listening": False, "listener_error": "Port in use"}

    prompt_test.proxy.state = state
    inference(monkeypatch, lambda _: pytest.fail("must not send inference"))
    with pytest.raises(v.VerificationError, match="Port in use"):
        asyncio.run(prompt_test.run(2))


@pytest.mark.parametrize("mode", ["stopped", "loading", "switching", "wrong_listener"])
def test_prompt_requires_ready_owned_target(prompt_test, probe, monkeypatch, mode):
    if mode == "stopped":
        probe.manager.status = lambda: {"state": "stopped"}
    elif mode == "loading":
        monkeypatch.setattr(probe, "readiness", lambda: {"ready": False, "reason": "Loading model"})
    elif mode == "switching":
        probe.gaming.state = lambda: {"blocked": True}
    else:
        monkeypatch.setattr(v, "owns_port", lambda _: False)
    inference(monkeypatch, lambda _: pytest.fail("must not send inference"))
    with pytest.raises(v.VerificationError):
        asyncio.run(prompt_test.run(2))


@pytest.mark.parametrize("replacement", ["process", "target"])
def test_prompt_rejects_replacement(prompt_test, monkeypatch, replacement):
    def handler(_):
        if replacement == "process":
            store.set_running({"pid": 123, "run_id": "replacement"})
        else:
            prompt_test.proxy.target = lambda: None
        return httpx.Response(200, json={"choices": [{"message": {"content": "old answer"}}]})

    inference(monkeypatch, handler)
    with pytest.raises(v.VerificationError, match="changed"):
        asyncio.run(prompt_test.run(2))


@pytest.mark.parametrize("body", [{}, [], {"choices": []}, {"choices": [{"message": {"content": ""}}]},
                                 {"choices": [{"message": {"content": ["invalid"]}}]}])
def test_prompt_rejects_invalid_generated_text(prompt_test, monkeypatch, body):
    inference(monkeypatch, lambda _: httpx.Response(200, json=body))
    with pytest.raises(v.VerificationError, match="invalid JSON or no generated text"):
        asyncio.run(prompt_test.run(2))


def test_prompt_retains_reasoning_only_and_length_result(prompt_test, monkeypatch):
    inference(monkeypatch, lambda _: httpx.Response(200, json={"choices": [{
        "message": {"reasoning_content": "thinking"}, "finish_reason": "length"}]}))
    result = asyncio.run(prompt_test.run(2))
    assert result["message"] == {"reasoning_content": "thinking"} and result["finish_reason"] == "length"


def test_prompt_has_overall_deadline_and_rejects_overlap(prompt_test, monkeypatch):
    async def scenario():
        started = asyncio.Event()

        async def handler(_):
            started.set()
            await asyncio.sleep(5)
            pytest.fail("inference should be cancelled")

        inference(monkeypatch, handler)
        first = asyncio.create_task(prompt_test.run(1))
        await asyncio.wait_for(started.wait(), 2)
        with pytest.raises(v.VerificationError, match="already running"):
            await prompt_test.run(1)
        with pytest.raises(v.VerificationTimeout, match="may still finish"):
            await first
        assert not prompt_test.lock.locked()

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [401, 429, 500])
def test_prompt_reports_upstream_errors_without_retry(prompt_test, monkeypatch, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "upstream failure"})

    inference(monkeypatch, handler)
    with pytest.raises(v.VerificationError, match=f"HTTP {status}"):
        asyncio.run(prompt_test.run(2))
    assert len(calls) == 1
