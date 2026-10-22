"""Build and exercise Compose with isolated ports, models, state, and cleanup.

Run: python tests/container_smoke.py [--gpu]
No real GGUF or GPU is required for the default CPU check.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def eventually(check, seconds=40):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.5)
    raise AssertionError("Timed out waiting for container smoke assertion")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gpu", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="llama-monitor-smoke-") as directory:
        Path(directory, "smoke.gguf").touch()
        env = {**os.environ, "MODELS_DIR": Path(directory).as_posix(),
               "BIND_ADDRESS": "127.0.0.1", "DASHBOARD_PORT": str(free_port()),
               "INFERENCE_PORT": str(free_port())}
        env.pop("LLAMA_CPP_IMAGE", None)
        compose = ["docker", "compose", "-p", f"llama-monitor-smoke-{os.getpid()}", "-f", "compose.yaml"]
        if args.gpu:
            compose += ["-f", "compose.override.yaml"]

        def run(*command, capture=False):
            return subprocess.run([*compose, *command], cwd=ROOT, env=env, check=True,
                                  text=True, stdout=subprocess.PIPE if capture else None).stdout

        def request(path, body=None, inference=False):
            port = env["INFERENCE_PORT" if inference else "DASHBOARD_PORT"]
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                data=json.dumps(body).encode() if body is not None else None,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as response:
                return json.load(response)

        def exec_python(source):
            return run("exec", "-T", "monitor", "python", "-c", source, capture=True).strip()

        try:
            run("config", "--quiet")
            run("up", "--build", "--wait", "--wait-timeout", "120", "-d")
            assert request("/api/health") == {"status": "ok"}
            assert exec_python("import os; print(os.getuid())") == "10001"
            state = request("/api/launcher/state")
            assert state["binary_valid"] and state["status"]["state"] == "stopped"
            bundled = state["settings"]["llama_server_path"]
            assert request("/api/launcher/flags")["source"] == "help"
            assert "/models/smoke.gguf" in request("/api/browse?path=/models&ext=.gguf")["files"]
            with urllib.request.urlopen(f"http://127.0.0.1:{env['DASHBOARD_PORT']}/") as response:
                html = response.read().decode()
            import re
            asset = re.search(r'src="([^"]+\.js)"', html)[1]
            with urllib.request.urlopen(f"http://127.0.0.1:{env['DASHBOARD_PORT']}{asset}") as response:
                assert response.status == 200
            source = (ROOT / "tests/fixtures/llama-server-smoke").read_text()
            exec_python("from pathlib import Path; p=Path('/tmp/llama-server-smoke'); "
                        f"p.write_text({source!r}); p.chmod(0o755)")
            request("/api/launcher/settings", {"llama_server_path": "/tmp/llama-server-smoke"})
            config = {"name": "smoke", "model_path": "/models/smoke.gguf", "port": 8001, "flags": []}
            request("/api/configs", config)
            assert request("/api/launcher/launch", config)["status"]["state"] == "running"
            eventually(lambda: request("/health", inference=True)["status"] == "ok")
            assert request("/v1/chat/completions", {"messages": []}, inference=True)["choices"]
            eventually(lambda: request("/api/history")["items"])
            assert request("/api/launcher/stop", {})["status"]["state"] == "stopped"
            assert exec_python("from pathlib import Path; print(Path('/data/smoke-stopped').read_text())") == "graceful"
            assert request("/api/launcher/restart", {})["status"]["state"] == "running"
            eventually(lambda: request("/health", inference=True)["status"] == "ok")
            exec_python("from pathlib import Path; Path('/data/smoke-stopped').unlink()")
            run("stop")
            assert run("ps", "--all", "--format", "json", capture=True)
            run("up", "--wait", "--wait-timeout", "60", "-d")
            assert exec_python("from pathlib import Path; print(Path('/data/smoke-stopped').read_text())") == "graceful"
            assert request("/api/launcher/state")["status"]["state"] == "stopped"
            request("/api/launcher/settings", {"llama_server_path": bundled})
            run("up", "--force-recreate", "--wait", "--wait-timeout", "60", "-d")
            assert request("/api/launcher/state")["binary_valid"]
            assert request("/api/configs")["configs"][0]["name"] == "smoke"
            assert request("/api/history")["items"]
            assert request("/api/launcher/state")["status"]["state"] == "stopped"
            print("Container smoke passed: assets, binary, browsing, launch/stop/restart, inference, history, persistence, graceful shutdown.")
        finally:
            run("logs", "--tail", "40")
            run("down", "--volumes", "--remove-orphans")


if __name__ == "__main__":
    main()
