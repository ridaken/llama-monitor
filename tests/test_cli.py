"""Exercise CLI contracts with an HTTP transport, without starting the backend."""
import io
import json
import subprocess
import sys

import httpx
import pytest

import cli


def test_application_log_and_metric_routes(invoke):
    code, _, calls = invoke(["app-logs", "--role", "embeddings", "--level", "warning", "--component", "launcher",
                             "--run", "run", "--since", "2h"], lambda _: {"items": []})
    assert code == 0 and calls[0].url.path == "/api/observability/events"
    params = calls[0].url.params
    assert params["role"] == "embeddings" and params["run_id"] == "run" and params["level"] == "warning"
    assert float(params["from_ts"]) > 0
    code, _, calls = invoke(["app-stats", "--model", "key", "--since", "7d"], lambda _: {"available": True})
    assert code == 0 and calls[0].url.path == "/api/observability/metrics"
    assert calls[0].url.params["model"] == "key"
    for arguments in (["app-logs", "--limit", "0"], ["app-stats", "--since", "bad"],
                      ["app-stats", "--since", "1h", "--from-ts", "1"]):
        code, _, calls = invoke(arguments, lambda _: pytest.fail("no request"))
        assert code == 2 and not calls


def test_log_follow_uses_forward_cursor_and_json_lines(monkeypatch, capsys):
    real_client = httpx.Client
    calls = []
    def dispatch(request):
        calls.append(request)
        return httpx.Response(200, json={"items": [], "next_cursor": str(len(calls)), "gap": False})
    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(dispatch)))
    def sleep(_):
        if len(calls) == 2:
            raise KeyboardInterrupt
    monkeypatch.setattr(cli.time, "sleep", sleep)
    assert cli.main(["app-logs", "--follow", "--json"]) == 130
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(lines) == 3 and lines[-1]["error"]["code"] == 130
    assert calls[0].url.params["direction"] == "forward"
    assert calls[1].url.params["cursor"] == "1"


def test_retained_log_cli_routes(invoke):
    code, output, calls = invoke(["logs", "--runs", "--limit", "10"], lambda _: {"items": [], "next_cursor": None})
    assert code == 0 and output["data"]["items"] == []
    assert calls[0].url.path == "/api/launcher/log-runs" and calls[0].url.params["limit"] == "10"
    code, output, calls = invoke(["logs", "--run", "a" * 32, "--tail", "1"],
        lambda _: {"available": True, "content": "first\nlast\n"})
    assert code == 0 and output["data"]["content"] == "last"
    assert calls[0].url.params["run_id"] == "a" * 32
    code, _, calls = invoke(["logs", "--runs", "--limit", "0"], lambda _: pytest.fail("no HTTP"))
    assert code == 2 and not calls


def test_embedding_role_and_wait_pin_operation_through_cpu_retry(invoke, tmp_path):
    config_path = tmp_path / "embedding.json"
    config_path.write_text(json.dumps({"model_path": "/models/embed.gguf", "port": 8081}))
    steps = iter([
        {"state": "waiting", "ready": False, "operation_id": "op", "run_id": None},
        {"state": "cpu_retry", "ready": False, "operation_id": "op", "run_id": "gpu-run"},
        {"state": "ready", "ready": True, "operation_id": "op", "run_id": "cpu-run"},
    ])
    def handler(request):
        assert request.url.params["role"] == "embeddings"
        if request.url.path.endswith("launch"):
            return {"status": {"operation_id": "op", "run_id": None}}
        return next(steps)
    code, output, _ = invoke(["launch", "--role", "embeddings", "--config-file", str(config_path), "--wait"], handler)
    assert code == 0 and output["data"]["readiness"]["run_id"] == "cpu-run"


def test_embedding_wait_rejects_replacement(invoke):
    steps = iter([
        {"state": "waiting", "ready": False, "operation_id": "first"},
        {"state": "ready", "ready": True, "operation_id": "replacement"},
    ])
    code, output, _ = invoke(["wait", "--ready", "--role", "embeddings"], lambda _: next(steps))
    assert code == 1 and "changed" in output["error"]["message"]


def test_embedding_configs_and_smoke_are_role_scoped(invoke):
    for args in (["configs", "list"], ["smoke"], ["logs", "--runs"], ["stop"]):
        code, _, calls = invoke([*args, "--role", "embeddings"], lambda _: {"configs": [], "items": []})
        assert code == 0 and all(c.url.params["role"] == "embeddings" for c in calls)


@pytest.fixture
def invoke(monkeypatch, capsys):
    real_client = httpx.Client

    def run(arguments, handler):
        calls = []

        def dispatch(request):
            calls.append(request)
            body = handler(request)
            return body if isinstance(body, httpx.Response) else httpx.Response(200, json=body)

        monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: real_client(
            **kwargs, transport=httpx.MockTransport(dispatch)))
        code = cli.main([*arguments, "--json"])
        output = capsys.readouterr()
        assert output.err == ""
        return code, json.loads(output.out), calls
    return run


@pytest.mark.parametrize("arguments,path", [
    (["stats"], "/api/stats"), (["gaming", "status"], "/api/gaming/state"),
    (["startup", "status"], "/api/startup/state"), (["stop"], "/api/launcher/stop"),
    (["restart"], "/api/launcher/restart"),
])
def test_routes(invoke, arguments, path):
    code, output, calls = invoke(arguments, lambda _: {"result": "received"})
    assert code == 0 and output == {"ok": True, "data": {"result": "received"}}
    assert calls[0].url.path == path
    assert calls[0].method == ("POST" if arguments[0] in ("restart", "stop") else "GET")


def test_activity_cli_filters_and_envelope(invoke):
    arguments = ["activity", "list", "--source", "proxy", "--model", "Model", "--endpoint", "/completion",
                 "--state", "complete", "--sort", "duration", "--order", "asc", "--from-ts", "1",
                 "--to-ts", "2", "--cursor", "opaque", "--limit", "10"]
    code, output, calls = invoke(arguments, lambda _: {"items": [], "next_cursor": None})
    assert code == 0 and output["ok"]
    assert calls[0].url.path == "/api/activity"
    assert dict(calls[0].url.params) == {"source": "proxy", "model": "Model", "endpoint": "/completion",
        "state": "complete", "sort": "duration", "order": "asc", "from_ts": "1.0", "to_ts": "2.0",
        "cursor": "opaque", "limit": "10"}
    code, _, calls = invoke(["activity", "list", "--limit", "0"], lambda _: pytest.fail("no HTTP"))
    assert code == 2 and not calls


def test_global_options_work_before_and_after_subcommands(invoke):
    code, _, calls = invoke(["--url", "http://example.test:9000", "--request-timeout", "7", "configs", "list"],
                            lambda _: {"configs": []})
    assert code == 0 and calls[0].url.host == "example.test"
    assert calls[0].extensions["timeout"]["read"] == 7


def test_status_succeeds_when_model_is_stopped(invoke):
    responses = {"/api/health": {"status": "ok"}, "/api/launcher/state": {"status": {"state": "stopped"}},
                 "/api/launcher/readiness": {"state": "stopped", "ready": False}}
    code, output, _ = invoke(["status"], lambda r: responses[r.url.path])
    assert code == 0 and output["data"]["readiness"]["ready"] is False


def test_launch_saved_config_waits_for_that_run(invoke):
    config = {"name": "test", "model_path": "/models/test.gguf", "port": 8001, "flags": []}
    responses = {"/api/configs": {"configs": [config]}, "/api/launcher/launch": {"status": {"run_id": "run1"}},
                 "/api/launcher/readiness": {"state": "ready", "ready": True, "run_id": "run1"}}
    code, output, calls = invoke(["launch", "--config", "test", "--wait"], lambda r: responses[r.url.path])
    assert code == 0 and output["data"]["readiness"]["ready"]
    assert json.loads(calls[1].content) == config
    assert len([r for r in calls if r.method == "POST"]) == 1


@pytest.mark.parametrize("source", ["file", "stdin"])
def test_unsaved_configuration_input(invoke, monkeypatch, tmp_path, source):
    config = {"model_path": "model with spaces.gguf", "port": 8001, "flags": []}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(config)))
    code, _, calls = invoke(["launch", "--config-file", "-" if source == "stdin" else str(path)], lambda _: {})
    assert code == 0 and json.loads(calls[0].content) == config


def test_invalid_config_never_posts(invoke, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("[]"))
    code, output, calls = invoke(["launch", "--config-file", "-"], lambda _: pytest.fail("no HTTP"))
    assert code == 2 and not output["ok"] and not calls


def test_wait_rejects_replaced_process(invoke, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    bodies = iter([{"state": "loading", "ready": False, "run_id": "a"},
                   {"state": "ready", "ready": True, "run_id": "b"}])
    code, output, _ = invoke(["wait", "--ready"], lambda _: next(bodies))
    assert code == 1 and "changed" in output["error"]["message"]


def test_backend_wait_retries_connection_then_succeeds(invoke, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectError("unreachable", request=request)
        return {"status": "ok"}

    code, _, _ = invoke(["wait", "--backend"], handler)
    assert code == 0 and len(attempts) == 2


def test_wait_deadline_is_bounded(invoke, monkeypatch):
    times = iter([0, 0, 2])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(times))
    code, _, calls = invoke(["wait", "--ready", "--timeout", "1"],
                            lambda _: {"state": "ready", "ready": True, "run_id": "a"})
    assert code == 4 and calls[0].extensions["timeout"]["read"] == 1


@pytest.mark.parametrize("state", ["stopped", "failed", "blocked"])
def test_terminal_wait_states_fail(invoke, state):
    code, _, _ = invoke(["wait", "--ready"], lambda _: {"state": state, "ready": False, "reason": state})
    assert code == 1


@pytest.mark.parametrize("response,expected", [
    (httpx.Response(400, json={"error": "rejected"}), 1),
    (httpx.Response(504, json={"error": "deadline"}), 4),
    (httpx.Response(200, text="html"), 3),
    (httpx.Response(200, json=[]), 3),
])
def test_errors(invoke, response, expected):
    code, output, _ = invoke(["stats"], lambda _: response)
    assert code == expected and not output["ok"]


@pytest.mark.parametrize("exception,expected", [(httpx.ConnectError, 3), (httpx.ReadTimeout, 4)])
def test_transport_errors_are_not_retried(invoke, exception, expected):
    def fail(request):
        raise exception("private transport details", request=request)
    code, output, calls = invoke(["restart"], fail)
    assert code == expected and len(calls) == 1
    assert "private" not in str(output)


@pytest.mark.parametrize("arguments", [["unknown"], ["launch"], ["smoke", "--timeout", "nan"],
    ["smoke", "--timeout", "301"], ["stats", "--request-timeout", "0"], ["logs", "--tail", "0"]])
def test_invalid_arguments_are_json(invoke, arguments):
    code, _, calls = invoke(arguments, lambda _: pytest.fail("must not call backend"))
    assert code == 2 and not calls


def test_history_filters_and_logs_tail(invoke):
    code, _, calls = invoke(["history", "list", "--model", "a b", "--from-ts", "12", "--cursor", "opaque"], lambda _: {})
    assert code == 0 and calls[0].url.params["model"] == "a b"
    assert calls[0].url.params["from_ts"] == "12.0"
    code, output, _ = invoke(["logs", "--tail", "2"], lambda _: {
        "available": True, "content": "a\nb\nc\n",
        "entries": [{"text": "a", "level": "debug"}, {"text": "b"}, {"text": "c"}],
    })
    assert code == 0 and output["data"] == {"available": True, "content": "b\nc"}


def test_smoke_has_explicit_deadline(invoke):
    code, _, calls = invoke(["smoke", "--timeout", "30"], lambda _: {"message": {"content": "OK"}})
    assert code == 0 and json.loads(calls[0].content) == {"timeout": 30}
    assert calls[0].extensions["timeout"]["read"] == 32


def test_test_prompt_uses_backend_route_and_default_deadline(invoke):
    code, output, calls = invoke(["test-prompt"], lambda _: {"route": "proxy", "message": {"content": "code"}})
    assert code == 0 and output["data"]["route"] == "proxy"
    assert len(calls) == 1 and calls[0].method == "POST" and calls[0].url.path == "/api/inference/test"
    assert json.loads(calls[0].content) == {"timeout": 120}
    assert calls[0].extensions["timeout"]["read"] == 122


@pytest.mark.parametrize("timeout", ["0", "301", "nan", "inf"])
def test_test_prompt_rejects_invalid_timeout(invoke, timeout):
    code, _, calls = invoke(["test-prompt", "--timeout", timeout], lambda _: pytest.fail("no HTTP"))
    assert code == 2 and not calls


@pytest.mark.parametrize("healthy", [True, False])
def test_doctor_preserves_report_and_exit_code(invoke, healthy):
    report = {"healthy": healthy, "summary": "diagnostic result", "checks": [], "recovery": []}
    code, output, calls = invoke(["doctor"], lambda _: report)
    assert code == (0 if healthy else 1)
    assert output["ok"] == healthy and output["data"] == report
    assert calls[0].url.path == "/api/launcher/doctor" and calls[0].method == "GET"


@pytest.mark.parametrize("url", ["file:///tmp/backend", "http://host:bad", "http://[bad", "http://user:password@host", "http://host/?query=1"])
def test_invalid_url_is_an_input_error(invoke, url):
    code, _, calls = invoke(["--url", url, "status"], lambda _: pytest.fail("no request"))
    assert code == 2 and not calls


def test_backend_wait_does_not_retry_protocol_errors(invoke):
    code, _, calls = invoke(["wait", "--backend"], lambda _: httpx.Response(200, text="not JSON"))
    assert code == 3 and len(calls) == 1


def test_url_option_overrides_environment(invoke, monkeypatch):
    monkeypatch.setenv("LLAMA_MONITOR_URL", "http://from-env:9001")
    code, _, calls = invoke(["configs", "list"], lambda _: {"configs": []})
    assert code == 0 and calls[0].url.host == "from-env"
    code, _, calls = invoke(["configs", "list", "--url", "http://explicit:9002"], lambda _: {"configs": []})
    assert code == 0 and calls[0].url.host == "explicit"


def test_real_entrypoint_has_help_and_json_error():
    assert subprocess.run([sys.executable, "cli.py", "--help"], capture_output=True).returncode == 0
    result = subprocess.run([sys.executable, "cli.py", "--json", "unknown"], capture_output=True, text=True)
    assert result.returncode == 2 and json.loads(result.stdout)["ok"] is False and result.stderr == ""


def test_proxy_settings_and_request_commands(invoke):
    code, _, calls = invoke(["proxy", "settings", "--enabled", "on", "--log-responses", "off"], lambda _: {})
    assert code == 0 and calls[0].url.path == "/api/proxy/settings"
    assert json.loads(calls[0].content) == {"enabled": True, "log_responses": False}
    code, _, calls = invoke(["proxy", "settings"], lambda _: {})
    assert code == 0 and calls[0].method == "GET"
    code, _, calls = invoke(["requests", "show", "identifier"], lambda _: {})
    assert code == 0 and calls[0].url.path == "/api/requests/identifier"
    code, _, calls = invoke(["requests", "clear", "--yes"], lambda _: {"deleted": 2})
    assert code == 0 and calls[0].method == "DELETE"
    code, _, calls = invoke(["requests", "list", "--model", "test", "--state", "cancelled"], lambda _: {})
    assert code == 0 and calls[0].url.params["state"] == "cancelled"


@pytest.mark.parametrize("arguments", [["requests", "clear"], ["requests", "list", "--limit", "0"],
    ["proxy", "settings", "--enabled", "yes"], ["proxy", "smoke", "--timeout", "301"],
    ["proxy", "smoke", "--api-key-env", "MISSING_PROXY_TEST_KEY"]])
def test_proxy_input_errors_never_contact_backend(invoke, arguments, monkeypatch):
    monkeypatch.delenv("MISSING_PROXY_TEST_KEY", raising=False)
    code, _, calls = invoke(arguments, lambda _: pytest.fail("must not contact backend"))
    assert code == 2 and not calls


def test_proxy_smoke_rejects_disabled_listener(invoke):
    code, _, calls = invoke(["proxy", "smoke"], lambda _: {"enabled": False, "listening": False})
    assert code == 1 and len(calls) == 1
