"""Detached log supervisor and a Popen-like handle for the actual inference PID."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import queue
import threading

import managed_logs as logs
from processes import capture_process, identity_matches


def detached():
    return ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
            if os.name == "nt" else {"start_new_session": True})


def identity(pid):
    record = capture_process(pid)
    return {k: v for k, v in record.items() if k not in ("argv", "cwd")}


class CapturedProcess:
    """Control inference directly; its supervisor only owns output and exit status."""
    def __init__(self, run_id, record, supervisor=None):
        self.run_id, self.pid = run_id, record["pid"]
        self.identity = record.get("identity")
        self.supervisor = supervisor
        self.returncode = None

    def poll(self):
        record = logs.load(self.run_id)
        if record.get("state") in ("exited", "interrupted"):
            self.returncode = record.get("exit_code")
            # Unknown exits remain distinguishable through the run manifest.
            return self.returncode if self.returncode is not None else 0
        if not self.supervisor or self.supervisor.poll() is not None:
            logs.reconcile()
            record = logs.load(self.run_id)
            if record.get("state") == "interrupted":
                return 0
        return None

    def wait(self, timeout=None):
        deadline = time.monotonic() + timeout if timeout is not None else float("inf")
        while self.poll() is None:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("llama-server", timeout)
            time.sleep(0.02)
        return self.returncode

    def _signal(self, force):
        import psutil
        if not self.identity:
            raise RuntimeError("Server process identity is unavailable; refusing to signal it.")
        if self.identity and identity_matches(self.identity):
            proc = psutil.Process(self.pid)
            try:
                if identity_matches(self.identity):
                    proc.kill() if force else proc.terminate()
            except psutil.NoSuchProcess:
                pass

    def terminate(self):
        self._signal(False)

    def kill(self):
        self._signal(True)


def launch(argv, cwd, run_id, config_name):
    logs.run_dir(run_id).mkdir(parents=True, exist_ok=True)
    supervisor = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), run_id], stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **detached())
    try:
        supervisor.stdin.write(json.dumps({"argv": argv, "cwd": cwd, "name": config_name,
                                          "managed_log": str(logs.store.MANAGED_LOG)}).encode())
        supervisor.stdin.close()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            record = logs.load(run_id)
            if record.get("pid"):
                return CapturedProcess(run_id, record, supervisor)
            if record.get("launch_error"):
                raise OSError(record["launch_error"])
            if supervisor.poll() is not None:
                raise OSError("Log capture exited before confirming the server launch.")
            time.sleep(0.02)
        # Do not abandon an unacknowledged supervisor that could load a model later.
        raise OSError("Log capture did not confirm the server launch within 10 seconds.")
    except Exception:
        # A cancellation marker is checked before launch and after spawning. The
        # supervisor remains the owner responsible for cleaning up its child.
        (logs.run_dir(run_id) / "cancel").touch()
        raise


def supervise(run_id):
    payload = json.loads(sys.stdin.buffer.read())
    logs.store.MANAGED_LOG = payload["managed_log"]
    writer = logs.Writer(run_id, payload.get("name"))
    writer.record["supervisor_identity"] = identity(os.getpid())
    writer.publish()
    child = None
    try:
        if (writer.directory / "cancel").exists():
            writer.finish(None, "launch_cancelled")
            return
        # File logging is monitor-owned: suppress that sink for both new and
        # restored legacy invocations. stdout/stderr provide the single stream.
        argv = list(payload["argv"])
        child = subprocess.Popen(argv, cwd=payload.get("cwd"), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **detached())
        try:
            actual = identity(child.pid)
        except Exception:
            actual = None  # a very early exit still has a retained Popen handle
        writer.record.update(pid=child.pid, identity=actual, state="running")
        writer.event("Server launched.")
        writer.publish()
        if (writer.directory / "cancel").exists():
            child.terminate()
        pending = queue.Queue(maxsize=16)
        def read_output():
            try:
                for line in logs.bounded_lines(child.stdout):
                    pending.put(line)
            finally:
                pending.put(None)
        threading.Thread(target=read_output, name="inference-output", daemon=True).start()
        last_intent = None
        while True:
            try:
                intent = json.loads((writer.directory / "stop.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                intent = None
            if intent and intent != last_intent:
                writer.record["stop_reason"] = intent["reason"]
                writer.event(f"Stop requested: {intent['reason']}."
                             + (" Escalating to forced termination." if intent.get("forced") else "")
                             + (" Stop failed; server ownership retained." if intent.get("failed") else ""))
                writer.publish()
                last_intent = intent
            if (writer.directory / "cancel").exists() and child.poll() is None:
                child.terminate()
            try:
                line = pending.get(timeout=0.1)
            except queue.Empty:
                continue
            if line is None:
                break
            raw, truncated = line
            try:
                writer.feed(raw.decode("utf-8", "replace"))
                if truncated:
                    writer.event("Oversized log line truncated.")
            except Exception as exc:
                # A broken disk or parser must never leave the inference pipe full.
                writer.fail(f"Log capture failed: {exc}")
                writer.publish()
        child.wait()
        writer.finish(child.returncode)
    except Exception as exc:
        writer.record["launch_error"] = str(exc)
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        writer.finish(child.returncode if child else None, "launch_failed")


if __name__ == "__main__":
    supervise(sys.argv[1])
