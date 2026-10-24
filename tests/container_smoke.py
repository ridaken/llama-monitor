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
        # Linux tempfile directories default to 0700; the container uses UID 10001.
        Path(directory).chmod(0o755)
        Path(directory, "smoke.gguf").touch()
        env = {**os.environ, "MODELS_DIR": Path(directory).as_posix(),
               "BIND_ADDRESS": "127.0.0.1", "DASHBOARD_PORT": str(free_port()),
               "INFERENCE_PORT": str(free_port()), "PROXY_PORT": str(free_port()), "PROXY_BIND_ADDRESS": "127.0.0.1"}
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

        def cli(*arguments, expected=0, input=None):
            result = subprocess.run([*compose, "exec", "-T", "monitor", "python", "cli.py", *arguments, "--json"],
                                    cwd=ROOT, env=env, input=input, capture_output=True, text=True)
            assert result.returncode == expected, (arguments, result.returncode, result.stdout, result.stderr)
            assert not result.stderr, result.stderr
            body = json.loads(result.stdout)
            assert body["ok"] == (expected == 0), body
            return body.get("data") if expected == 0 else body["error"]

        try:
            run("config", "--quiet")
            run("up", "--build", "--wait", "--wait-timeout", "120", "-d")
            assert request("/api/health") == {"status": "ok"}
            cli("wait", "--backend", "--timeout", "10")
            report = cli("doctor")
            assert report["healthy"] and report["checks"][0]["state"] == "pass"
            if args.gpu:
                assert report["checks"][1]["state"] == "pass"
            assert cli("status")["readiness"]["state"] == "stopped"
            cli("wait", "--ready", expected=1)
            cli("smoke", expected=1)
            cli("launch", expected=2)
            cli("stats", "--url", "http://127.0.0.1:9", expected=3)
            cli("wait", "--backend", "--timeout", "0.1", "--url", "http://127.0.0.1:9", expected=4)
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
            # Keep the fake executable across Compose container recreation.
            exec_python("from pathlib import Path; p=Path('/data/llama-server-smoke'); "
                        f"p.write_text({source!r}); p.chmod(0o755)")
            request("/api/launcher/settings", {"llama_server_path": "/data/llama-server-smoke"})
            config = {"name": "smoke", "model_path": "/models/smoke.gguf", "port": 8001, "flags": []}
            request("/api/configs", config)
            assert cli("configs", "list")["configs"][0] == config
            assert cli("configs", "show", "smoke") == config
            assert cli("launch", "--config", "smoke", "--wait", "--timeout", "30")["readiness"]["ready"]
            eventually(lambda: request("/health", inference=True)["status"] == "ok")
            assert cli("smoke", "--timeout", "10")["message"]["content"] == "smoke response"
            assert not cli("proxy", "status")["enabled"]
            direct = cli("test-prompt", "--timeout", "10")
            assert direct["route"] == "direct" and direct["message"]["content"] == "smoke response"
            assert direct["request_id"] is None and cli("requests", "list")["items"] == []
            cli("proxy", "smoke", expected=1)
            assert cli("proxy", "settings", "--enabled", "on")["listening"]
            probe = cli("test-prompt", "--timeout", "10")
            assert probe["route"] == "proxy"
            eventually(lambda: cli("requests", "show", probe["request_id"])["state"] == "complete")
            assert "FizzBuzz" in cli("requests", "show", probe["request_id"])["request_text"]
            probe = cli("proxy", "smoke", "--stream", "--timeout", "10")
            assert probe["response"]["choices"][0]["content"] == "smoke response"
            assert probe["first_chunk_seconds"] < probe["elapsed_seconds"] - 0.1
            eventually(lambda: cli("requests", "show", probe["request_id"])["state"] == "complete")
            saved = cli("requests", "show", probe["request_id"])
            assert "Reply with OK" in saved["request_text"] and "smoke response" in saved["response_text"]
            assert cli("proxy", "settings", "--log-responses", "off")["log_responses"] is False
            probe = cli("proxy", "smoke", "--timeout", "10")
            eventually(lambda: cli("requests", "show", probe["request_id"])["state"] == "complete")
            assert cli("requests", "show", probe["request_id"])["response_text"] is None
            assert cli("requests", "list")["items"]
            cli("requests", "clear", expected=2)
            cli("requests", "clear", "--yes")
            assert cli("requests", "list")["items"] == []
            assert cli("proxy", "settings", "--enabled", "off")["listening"] is False
            assert request("/health", inference=True)["status"] == "ok"
            assert cli("stats")["runtime"]["containerized"]
            eventually(lambda: cli("stats")["layer_placement"]["status"] == "available")
            memory = cli("stats")
            assert memory["split_source"] == "log"
            assert memory["split"] == [{"label": "CPU", "bytes": 1024**2, "kind": "cpu"}]
            placement = cli("stats")["layer_placement"]
            assert placement["total_layers"] == 2 and placement["output_device"] == "CPU"
            assert placement["devices"] == [{"label": "CPU", "layers": 2}]
            assert cli("logs", "--tail", "2")["available"]
            assert cli("gaming", "status")["supported"] is False
            assert cli("startup", "status")["supported"] is False
            eventually(lambda: cli("history", "list")["items"])
            assert cli("stop")["status"]["state"] == "stopped"
            assert cli("stats")["layer_placement"]["status"] == "no_model"
            assert exec_python("from pathlib import Path; print(Path('/data/smoke-stopped').read_text())") == "graceful"
            assert cli("launch", "--config-file", "-", "--wait", "--timeout", "30",
                       input=json.dumps(config))["readiness"]["ready"]
            assert cli("restart", "--wait", "--timeout", "30")["readiness"]["ready"]
            eventually(lambda: request("/health", inference=True)["status"] == "ok")
            restarted = cli("status")["readiness"]["run_id"]
            cli("smoke", "--timeout", "10")
            # No stats/UI request: the new run must already be filterable by model.
            eventually(lambda: any(row["run_id"] == restarted and row["state"] == "complete"
                                   for row in cli("history", "list", "--model", "smoke.gguf")["items"]))
            exec_python("from pathlib import Path; Path('/data/smoke-stopped').unlink()")
            run("stop")
            assert run("ps", "--all", "--format", "json", capture=True)
            run("up", "--wait", "--wait-timeout", "60", "-d")
            assert exec_python("from pathlib import Path; print(Path('/data/smoke-stopped').read_text())") == "graceful"
            assert cli("status")["launcher"]["status"]["state"] == "stopped"
            # Opt-in startup must work without a browser or a manual launch.
            request("/api/configs/default", {"name": "smoke"})
            request("/api/launcher/settings", {"autostart_default_model": True})
            run("up", "--force-recreate", "--wait", "--wait-timeout", "60", "-d")
            cli("wait", "--backend", "--timeout", "30")
            startup_state = request("/api/launcher/state")
            assert startup_state["autostart_error"] is None, startup_state["autostart_error"]
            assert cli("wait", "--ready", "--timeout", "30")["ready"]
            auto_run = cli("status")["launcher"]["status"]["run_id"]
            assert auto_run and auto_run != restarted
            assert cli("status")["launcher"]["settings"]["autostart_default_model"] is True
            assert cli("stop")["status"]["state"] == "stopped"
            # Allow several coordinator/poll intervals to expose accidental retries.
            stopped_until = time.monotonic() + 4
            while time.monotonic() < stopped_until:
                assert cli("status")["launcher"]["status"]["state"] == "stopped"
                time.sleep(.25)
            request("/api/launcher/settings", {"autostart_default_model": False})
            # A loader may fail before --log-file is initialized, without even
            # a trailing newline. Both the console and doctor must retain it.
            exec_python("from pathlib import Path; p=Path('/tmp/llama-server-crash'); "
                        "p.write_text('#!/opt/venv/bin/python\\nimport sys\\nsys.stderr.write(\"early loader fault\")\\nsys.exit(7)\\n'); p.chmod(0o755)")
            request("/api/launcher/settings", {"llama_server_path": "/tmp/llama-server-crash"})
            cli("launch", "--config", "smoke")
            eventually(lambda: cli("status")["launcher"]["status"]["state"] == "exited")
            assert cli("logs")["content"] == "early loader fault"
            cli("doctor", expected=1)
            cli("wait", "--ready", expected=1)
            request("/api/launcher/settings", {"llama_server_path": bundled})
            run("up", "--force-recreate", "--wait", "--wait-timeout", "60", "-d")
            assert request("/api/launcher/state")["binary_valid"]
            assert cli("configs", "list")["configs"][0]["name"] == "smoke"
            assert cli("history", "list")["items"]
            assert cli("status")["launcher"]["status"]["state"] == "stopped"
            print("Container smoke passed: bundled CLI, exit codes, readiness, inference, telemetry, logs, history, lifecycle, persistence, graceful shutdown.")
        finally:
            run("logs", "--tail", "40")
            run("down", "--volumes", "--remove-orphans")


if __name__ == "__main__":
    main()
