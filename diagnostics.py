"""Read-only, bounded server/GPU diagnostics. Never loads a model or restarts Docker."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import runtime
import store
from launcher import resolve_binary

PROBE_TIMEOUT = 4
OUTPUT_LIMIT = 16 * 1024


def probe(name, argv):
    # Bound response size without risking a full pipe deadlock. No shell, user
    # model flags, credentials, or inference requests are involved.
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=output,
                                    stderr=subprocess.STDOUT, timeout=PROBE_TIMEOUT,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            code = result.returncode
            state = "pass" if code == 0 else "fail"
            message = "Check passed." if code == 0 else f"Process exited with code {code}."
        except subprocess.TimeoutExpired:
            code, state, message = None, "fail", f"Check timed out after {PROBE_TIMEOUT} seconds."
        except OSError as exc:
            code, state, message = None, "fail", f"Cannot run check: {exc}"
        size = output.seek(0, os.SEEK_END)
        output.seek(max(0, size - OUTPUT_LIMIT))
        text = output.read(OUTPUT_LIMIT).decode("utf-8", "replace").strip()
    return {"name": name, "state": state, "exit_code": code, "message": message,
            "output": text, "truncated": size > OUTPUT_LIMIT}


def doctor():
    binary = resolve_binary(store.get_settings().get("llama_server_path"))
    checks = []
    if binary:
        checks.append(probe("server_devices", [binary, "--list-devices"]))
    else:
        checks.append({"name": "server_devices", "state": "fail", "exit_code": None,
                       "message": "Set a valid llama-server executable in Manage.", "output": ""})
    smi = shutil.which("nvidia-smi")
    if smi:
        checks.append(probe("nvidia", [smi, "--query-gpu=name,driver_version", "--format=csv,noheader"]))
    else:
        checks.append({"name": "nvidia", "state": "skipped", "exit_code": None,
                       "message": "nvidia-smi is unavailable; NVIDIA access was not verified.", "output": ""})
    healthy = all(check["state"] != "fail" for check in checks)
    nvidia_failed = any(check["name"] == "nvidia" and check["state"] == "fail" for check in checks)
    recovery = []
    if runtime.containerized() and nvidia_failed:
        recovery = [
            "NVIDIA access failed inside the container. On Windows with Docker Desktop/WSL, GPU access may need to be refreshed.",
            "Stop Docker Desktop, run wsl --shutdown in PowerShell, then start Docker Desktop and run docker compose up -d.",
            "This interrupts all Docker containers and active WSL sessions. Save their work first; no restart is performed automatically.",
            "Keep WSL, Docker Desktop, and the Windows NVIDIA driver up to date. Run diagnostics again before retrying the model.",
        ]
    return {"healthy": healthy, "summary": "Diagnostic checks passed. This does not verify model compatibility or inference."
            if healthy else "Diagnostic checks failed. Review the check output before retrying the model.",
            "containerized": runtime.containerized(), "checks": checks, "recovery": recovery}
