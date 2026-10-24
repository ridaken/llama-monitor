"""Real sockets verify forwarding, streaming, cancellation and listener lifecycle."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

import app as app_module
from history import HistoryDB
from proxy_capture import MAX_PAYLOAD, Capture, ResponseCapture
from proxy_history import RequestHistory
import store


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def eventually(check):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError("Request history did not reach expected state")


@pytest.fixture
def upstream():
    received, disconnected = [], threading.Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def respond(self, data, code=200):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "https://client.test")
            self.send_header("Connection", "keep-alive, x-private")
            self.send_header("X-Private", "remove me")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            received.append({"path": self.path, "method": "GET"})
            self.respond(b'{"status":"ok"}')

        def do_OPTIONS(self):
            self.respond(b'{}')

        def do_POST(self):
            if self.headers.get("Transfer-Encoding") == "chunked":
                chunks = []
                while True:
                    size = int(self.rfile.readline().strip(), 16)
                    if not size:
                        self.rfile.readline()
                        break
                    chunks.append(self.rfile.read(size))
                    self.rfile.read(2)
                raw = b"".join(chunks)
            else:
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            received.append({"path": self.path, "raw": raw, "headers": dict(self.headers)})
            body = json.loads(raw)
            if body.get("error"):
                return self.respond(b'{"error":{"message":"upstream error"}}', 429)
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                frames = [
                    {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                    {"choices": [{"index": 0, "delta": {"reasoning_content": "Think "}}]},
                    {"choices": [{"index": 0, "delta": {"content": "hello 🦙"}}]},
                    {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call1", "type": "function", "function": {"name": "lookup", "arguments": "{\"x\":"}}]}}]},
                    {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 4, "completion_tokens": 5}},
                ]
                try:
                    for frame in frames:
                        raw_frame = ("data: " + json.dumps(frame, ensure_ascii=False) + "\r\n\r\n").encode()
                        for offset in range(0, len(raw_frame), 3):
                            self.wfile.write(raw_frame[offset:offset + 3])
                            self.wfile.flush()
                        time.sleep(0.04)
                    if body.get("endless"):
                        for _ in range(200):
                            self.wfile.write(b'data: {"choices":[{"delta":{"content":"more"}}]}\n\n')
                            self.wfile.flush()
                            time.sleep(0.02)
                    elif not body.get("incomplete"):
                        self.wfile.write(b'data: [DONE]\n\n')
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    disconnected.set()
                self.close_connection = True
                return
            if self.path == "/completion":
                return self.respond(b'{"content":"native answer","stop":true,"timings":{"prompt_n":3,"predicted_n":2}}')
            self.respond(json.dumps({"model": "test-model", "choices": [{"index": 0, "message": {
                "content": body.get("answer", "hello"), "reasoning_content": "thoughts"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 5}}).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", received, disconnected
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture
def backend(monkeypatch, tmp_path, upstream):
    monkeypatch.setattr(store, "HOME_DIR", str(tmp_path))
    monkeypatch.setattr(store, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(store, "MANAGED_LOG", str(tmp_path / "server.log"))
    args = argparse.Namespace(llama_url=upstream[0], llama_log=None, port=free_port(),
                              host="127.0.0.1", proxy_port=free_port(), test_mode=True)
    application = app_module.build_app(args)
    with TestClient(application) as api:
        yield api, f"http://127.0.0.1:{args.proxy_port}", application.state.proxy


def enable(api):
    state = api.post("/api/proxy/settings", json={"enabled": True}).json()
    assert state["enabled"] and state["listening"], state


def record(api, identifier):
    return eventually(lambda: (row if (row := api.get(f"/api/requests/{identifier}").json()).get("state") != "running" else None))


def test_roundtrip_forwarding_and_capture(backend, upstream):
    api, url, _ = backend
    assert not api.get("/api/proxy/state").json()["enabled"]
    enable(api)
    raw = b'{"model":"requested","messages":[{"role":"user","content":"my prompt"}]}'
    response = httpx.post(url + "/v1/chat/completions?x=a%2Fb&x=c", content=raw,
        headers={"Content-Type": "application/json", "Authorization": "Bearer secret", "Connection": "x-hide", "X-Hide": "hidden"})
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "hello"
    assert response.headers["access-control-allow-origin"] == "https://client.test"
    assert "x-private" not in response.headers
    received = upstream[1][-1]
    assert received["raw"] == raw and received["path"].endswith("?x=a%2Fb&x=c")
    headers = {key.lower(): value for key, value in received["headers"].items()}
    assert headers["authorization"] == "Bearer secret"
    assert "x-hide" not in headers
    identifier = response.headers["x-llama-monitor-request-id"]
    row = record(api, identifier)
    assert row["state"] == "complete" and row["model"] == "test-model"
    assert row["usage"]["completion_tokens"] == 5
    assert "my prompt" in row["request_text"] and "thoughts" in row["response_text"]
    assert "secret" not in json.dumps(row)
    listing = api.get("/api/requests?model=test-model").json()
    assert len(listing["items"]) == 1 and "response_text" not in listing["items"][0]
    assert api.get("/api/history").json()["items"] == []


def test_stream_is_live_and_assembles_reasoning_tools(backend):
    api, url, _ = backend
    enable(api)
    started = time.monotonic()
    with httpx.stream("POST", url + "/v1/chat/completions", json={"stream": True}) as response:
        chunks = response.iter_raw()
        assert next(chunks).startswith(b"dat")
        first = time.monotonic() - started
        tail = b"".join(chunks)
    assert b"[DONE]" in tail
    assert time.monotonic() - started - first > 0.1
    row = record(api, response.headers["x-llama-monitor-request-id"])
    body = json.loads(row["response_text"])
    assert body["choices"][0]["content"] == "hello 🦙"
    assert body["choices"][0]["reasoning_content"] == "Think "
    assert body["choices"][0]["tool_calls"][0]["function"] == {"name": "lookup", "arguments": '{"x":1}'}
    assert row["state"] == "complete" and row["first_token_seconds"] > 0


def test_cancel_closes_upstream_and_keeps_partial_output(backend, upstream):
    api, url, _ = backend
    enable(api)
    with httpx.stream("POST", url + "/v1/chat/completions", json={"stream": True, "endless": True}) as response:
        for line in response.iter_lines():
            if "hello" in line:
                break
    assert upstream[2].wait(4)
    row = record(api, response.headers["x-llama-monitor-request-id"])
    assert row["state"] == "cancelled" and "hello" in row["response_text"]


def test_incomplete_and_upstream_errors(backend):
    api, url, _ = backend
    enable(api)
    for body, expected in (({"stream": True, "incomplete": True}, "incomplete"), ({"error": True}, "error")):
        response = httpx.post(url + "/v1/chat/completions", json=body)
        row = record(api, response.headers["x-llama-monitor-request-id"])
        assert row["state"] == expected
        if expected == "error":
            assert response.status_code == 429 and response.json()["error"]["message"] == "upstream error"


def test_toggles_and_non_capture_paths(backend):
    api, url, _ = backend
    enable(api)
    api.post("/api/proxy/settings", json={"log_responses": False, "log_prompts": False})
    response = httpx.post(url + "/v1/completions", json={"prompt": "private"})
    row = record(api, response.headers["x-llama-monitor-request-id"])
    assert row["request_text"] is None and row["response_text"] is None
    assert row["usage"]["prompt_tokens"] == 4
    before = len(api.get("/api/requests").json()["items"])
    assert httpx.get(url + "/health").json() == {"status": "ok"}
    assert httpx.options(url + "/anything").headers["access-control-allow-origin"]
    assert httpx.post(url + "/v1/responses", json={}).status_code == 200
    assert len(api.get("/api/requests").json()["items"]) == before


def test_logging_failure_does_not_fail_inference(backend, monkeypatch):
    api, url, proxy = backend
    enable(api)
    def broken(*args):
        raise OSError("private filesystem detail")
    monkeypatch.setattr(proxy.history, "begin", broken)
    assert httpx.post(url + "/v1/chat/completions", json={}).status_code == 200
    state = api.get("/api/proxy/state").json()
    assert "capture failed" in state["capture_error"]
    assert "private filesystem" not in state["capture_error"]


def test_lifecycle_port_failure_and_loop(backend):
    api, url, proxy = backend
    with socket.create_server(("127.0.0.1", proxy.port)):
        state = api.post("/api/proxy/settings", json={"enabled": True}).json()
        assert state["listener_error"] and not state["listening"]
        assert api.get("/api/health").json() == {"status": "ok"}
    enable(api)
    original = proxy.target
    proxy.target = lambda: {"url": url}
    assert httpx.get(url + "/health").status_code == 502
    proxy.target = lambda: {"url": f"http://[::ffff:127.0.0.1]:{proxy.port}"}
    assert httpx.get(url + "/health").status_code == 502
    proxy.target = lambda: None
    assert httpx.post(url + "/v1/chat/completions", json={}).status_code == 503
    proxy.target = original
    state = api.post("/api/proxy/settings", json={"enabled": False}).json()
    assert not state["listening"]
    enable(api)


def test_settings_validate_and_history_filters(backend):
    api, url, _ = backend
    for body in ({"enabled": "yes"}, {"unknown": False}, [], None):
        assert api.post("/api/proxy/settings", json=body).status_code == 400
    enable(api)
    for _ in range(3):
        response = httpx.post(url + "/completion", json={"prompt": "native"})
        assert record(api, response.headers["x-llama-monitor-request-id"])["usage"]["completion_tokens"] == 2
    first = api.get("/api/requests?limit=2&endpoint=/completion&state=complete").json()
    second = api.get("/api/requests", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert len(first["items"]) == 2 and len(second["items"]) == 1
    for query in ("cursor=bad", "limit=0", "from_ts=nan"):
        assert api.get("/api/requests?" + query).status_code == 400
    assert api.delete("/api/requests").json()["deleted"] == 3
    assert api.get("/api/requests").json()["items"] == []


def test_database_clear_and_restart(tmp_path):
    db = HistoryDB(str(tmp_path / "history.sqlite"))
    history = RequestHistory(db)
    row = {"id": "one", "observed_at": time.time(), "endpoint": "/completion", "state": "running"}
    history.begin(row)
    assert RequestHistory(db).get("one")["state"] == "interrupted"
    history.clear()
    row["state"] = "complete"
    history.finish(row, "prompt", "response")
    assert history.get("one") is None
    # A delayed initial insert from before clear cannot recreate old content.
    history.begin(row)
    assert history.get("one") is None


def test_bounded_capture_and_media():
    capture = Capture()
    capture.feed(json.dumps({"messages": [{"type": "image_url", "image_url": {"url": "data:secret"}}]}).encode())
    assert "data:secret" not in capture.request()[0]
    assert "omitted" in capture.request()[0]
    capture.feed(b"x" * (MAX_PAYLOAD + 5))
    assert len(capture.raw) == MAX_PAYLOAD and capture.request()[2]
    response = ResponseCapture(True, time.monotonic())
    response.sse = True
    response.feed(b"data: " + b"x" * (MAX_PAYLOAD + 5))
    assert response.truncated and response.invalid


def test_capture_limits_do_not_truncate_forwarded_response(backend):
    api, url, _ = backend
    enable(api)
    answer = "x" * (MAX_PAYLOAD + 10)
    response = httpx.post(url + "/v1/chat/completions", json={"answer": answer})
    assert response.json()["choices"][0]["message"]["content"] == answer
    row = record(api, response.headers["x-llama-monitor-request-id"])
    assert row["state"] == "complete"
    assert row["request_truncated"] and row["response_truncated"]
    assert len(row["response_text"].encode()) <= MAX_PAYLOAD


def test_concurrent_requests_are_paired_without_log_matching(backend):
    api, url, _ = backend
    enable(api)
    def send(number):
        response = httpx.post(url + "/v1/chat/completions", json={"messages": [{"content": str(number)}], "answer": str(number)})
        return number, response.headers["x-llama-monitor-request-id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        identifiers = list(pool.map(send, range(8)))
    assert len({identifier for _, identifier in identifiers}) == 8
    for number, identifier in identifiers:
        row = record(api, identifier)
        assert json.loads(row["request_text"])["messages"][0]["content"] == str(number)
        assert json.loads(row["response_text"])["choices"][0]["message"]["content"] == str(number)


def test_target_and_logging_are_pinned_and_clear_during_inference(backend, upstream):
    api, url, proxy = backend
    enable(api)
    with httpx.stream("POST", url + "/v1/chat/completions", json={"stream": True}) as response:
        iterator = response.iter_raw()
        next(iterator)
        proxy.target = lambda: {"url": "http://127.0.0.1:9", "run_id": "new"}
        api.post("/api/proxy/settings", json={"log_responses": False})
        b"".join(iterator)
    row = record(api, response.headers["x-llama-monitor-request-id"])
    assert row["state"] == "complete" and row["target"] == upstream[0]
    assert row["response_logging"] and "hello" in row["response_text"]
    # Subsequent requests use the new target and report a gateway failure.
    failure = httpx.post(url + "/v1/chat/completions", json={})
    assert failure.status_code == 502
    proxy.target = lambda: {"url": upstream[0]}
    with httpx.stream("POST", url + "/v1/chat/completions", json={"stream": True}) as response:
        iterator = response.iter_raw()
        next(iterator)
        api.delete("/api/requests")
        b"".join(iterator)
    # Finish is an UPDATE, never an insert, including a real in-flight request.
    assert api.get("/api/requests").json()["items"] == []


@pytest.mark.parametrize("frames,expected", [
    ([{"choices": [{"index": 0, "text": "text "}]}, {"choices": [{"index": 0, "text": "completion", "finish_reason": "stop"}]}], "text completion"),
    ([{"content": "native "}, {"content": "stream", "stop": True, "timings": {"prompt_n": 1, "predicted_n": 2}}], "native stream"),
])
def test_other_completion_stream_shapes(frames, expected):
    response = ResponseCapture(True, time.monotonic())
    response.sse = True
    for frame in frames:
        data = ("data: " + json.dumps(frame) + "\n\n").encode()
        for offset in range(len(data)):
            response.feed(data[offset:offset + 1])
    response.feed(b"data: [DONE]\n\n")
    assert expected in response.finish()
    assert response.done and not response.invalid


def test_empty_content_has_no_invented_token_usage():
    response = ResponseCapture(False, time.monotonic())
    response.feed(b'{"choices":[{"message":{"content":""}}]}')
    assert response.finish() is None
    assert response.usage is None and response.first_token is None
