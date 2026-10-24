"""Exercise the real CLI against an isolated native backend and fake upstream.

No real model, GPU, normal state, or running installation is used.
Run: python tests/native_proxy_smoke.py
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def eventually(check):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("Native proxy smoke assertion timed out")


def main():
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if body.get("stream") else "application/json")
            self.end_headers()
            if body.get("stream"):
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"native "}}]}\n\n')
                self.wfile.flush()
                time.sleep(0.25)
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"answer"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
            else:
                self.wfile.write(b'{"choices":[{"message":{"content":"native answer"},"finish_reason":"stop"}]}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    (ROOT / "test-results").mkdir(exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="proxy-native-", dir=ROOT / "test-results") as directory:
            port, proxy_port = free_port(), free_port()
            env = {**os.environ, "LLAMA_MONITOR_DATA_DIR": directory}
            env.pop("LLAMA_MONITOR_PROXY_URL", None)
            base = f"http://127.0.0.1:{port}"
            process = None

            def cli(*arguments, expected=0):
                result = subprocess.run([sys.executable, "cli.py", "--url", base, *arguments, "--json"],
                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=20)
                assert result.returncode == expected, (arguments, result.returncode, result.stdout, result.stderr)
                assert not result.stderr, result.stderr
                result = json.loads(result.stdout)
                assert result["ok"] == (expected == 0)
                return result.get("data", result.get("error"))

            def start(log):
                return subprocess.Popen([sys.executable, "app.py", "--test-mode", "--port", str(port),
                    "--proxy-port", str(proxy_port), "--llama-url", f"http://127.0.0.1:{server.server_port}"],
                    cwd=ROOT, env=env, stdout=log, stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

            def stop():
                if process and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)

            with open(Path(directory) / "backend.log", "w+") as log:
                try:
                    process = start(log)
                    cli("wait", "--backend", "--timeout", "10")
                    assert cli("status")["readiness"]["state"] == "stopped"
                    assert not cli("proxy", "status")["enabled"]
                    cli("proxy", "smoke", expected=1)
                    assert cli("proxy", "settings", "--enabled", "on")["listening"]
                    for streaming in (False, True):
                        args = ["--stream"] if streaming else []
                        probe = cli("proxy", "smoke", *args, "--timeout", "5")
                        if streaming:
                            assert probe["first_chunk_seconds"] < probe["elapsed_seconds"] - 0.15
                        eventually(lambda: cli("requests", "show", probe["request_id"])["state"] == "complete")
                        row = cli("requests", "show", probe["request_id"])
                        assert "Reply with OK" in row["request_text"]
                        assert "native answer" in row["response_text"]
                        assert row["usage"] is None
                    cli("proxy", "settings", "--log-responses", "off")
                    probe = cli("proxy", "smoke", "--stream")
                    eventually(lambda: cli("requests", "show", probe["request_id"])["state"] == "complete")
                    assert cli("requests", "show", probe["request_id"])["response_text"] is None
                    assert len(cli("requests", "list")["items"]) == 3
                    cli("requests", "clear", expected=2)
                    assert cli("requests", "clear", "--yes")["deleted"] == 3
                    assert cli("requests", "list")["items"] == []
                    cli("proxy", "settings", "--enabled", "off")
                    stop()
                    process = start(log)
                    cli("wait", "--backend", "--timeout", "10")
                    state = cli("proxy", "status")
                    assert not state["enabled"] and not state["log_responses"]
                    cli("proxy", "settings", "--enabled", "on")
                    cli("proxy", "smoke", "--timeout", "0.05", "--stream", expected=4)
                    cli("proxy", "settings", "--enabled", "off")
                    print("Native proxy smoke passed: real CLI, live streaming, request pairs, capture toggles, clearing, restart persistence, timeout exit codes.")
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read(), file=sys.stderr)
                    raise
                finally:
                    stop()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    main()
