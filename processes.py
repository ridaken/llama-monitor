"""Process identity and exact-invocation ownership for registered AI servers."""
from __future__ import annotations

import os
import subprocess

import psutil


class ProcessError(RuntimeError):
    pass


def same_path(a, b):
    return bool(a and b) and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def capture_process(pid):
    p = psutil.Process(pid)
    with p.oneshot():
        argv = p.cmdline()
        exe = p.exe()
        return {"pid": pid, "created_at": p.create_time(), "executable": exe,
                "argv": [exe, *argv[1:]], "cwd": p.cwd()}


def identity_matches(record):
    """False only for absence/reuse; access errors must not imply process exit."""
    if not record or not record.get("created_at") or not record.get("executable"):
        raise ProcessError("Process identity is unavailable; refusing to control it.")
    try:
        p = psutil.Process(record["pid"])
        return (p.create_time() == record["created_at"]
                and same_path(p.exe(), record["executable"])
                and p.status() != psutil.STATUS_ZOMBIE)
    except psutil.NoSuchProcess:
        return False


def terminate_verified(record, grace=5):
    if not identity_matches(record):
        return
    p = psutil.Process(record["pid"])
    try:
        # Recheck immediately before sending a signal to a numeric PID.
        if not identity_matches(record):
            return
        p.terminate()
        try:
            p.wait(timeout=grace)
        except psutil.TimeoutExpired:
            if identity_matches(record):
                p.kill()
                p.wait(timeout=grace)
    except psutil.NoSuchProcess:
        return
    if identity_matches(record):
        raise ProcessError("AI process did not exit after termination.")


def spawn_exact(record):
    kwargs = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
              if os.name == "nt" else {"start_new_session": True})
    return subprocess.Popen(record["argv"], cwd=record["cwd"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, **kwargs)


def argv_port(argv):
    port = 8080
    for i, value in enumerate(argv):
        if value == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1])
        if value.startswith("--port="):
            port = int(value.partition("=")[2])
    return port


def discover_registered(executable, port):
    """Find only the explicitly registered executable and inference port."""
    found = []
    for p in psutil.process_iter(["name"]):
        if (p.info["name"] or "").lower() not in ("llama-server", "llama-server.exe"):
            continue
        try:
            record = capture_process(p.pid)
            if same_path(record["executable"], executable) and argv_port(record["argv"]) == port:
                alias = None
                model = None
                for i, arg in enumerate(record["argv"][:-1]):
                    if arg in ("--alias", "-a"):
                        alias = record["argv"][i + 1]
                    if arg in ("--model", "-m"):
                        model = record["argv"][i + 1]
                record.update(id="embeddings", port=port, name=alias or os.path.basename(model or "Embeddings"))
                found.append(record)
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            raise ProcessError("Cannot inspect a llama-server process under this Windows account.") from exc
    if len(found) > 1:
        raise ProcessError("More than one server matches the registered embedding port.")
    return found[0] if found else None


class AuxiliaryServer:
    def __init__(self):
        self.proc = None
        self.record = None

    def snapshot(self, registration):
        if self.record and self.alive(self.record):
            return dict(self.record)
        self.record = discover_registered(registration["executable"], registration["port"])
        return dict(self.record) if self.record else None

    def alive(self, record):
        if self.proc and self.proc.pid == record["pid"]:
            return self.proc.poll() is None and identity_matches(record)
        return identity_matches(record)

    def stop(self, record):
        if self.proc and (record is None or self.proc.pid == record["pid"]):
            # A freshly spawned child's retained Popen handle also permits
            # cleanup if identity inspection failed immediately after spawn.
            if self.proc.poll() is None:
                if record and not identity_matches(record) and self.proc.poll() is None:
                    raise ProcessError("Embedding process identity changed; refusing to signal it.")
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
            if self.proc.poll() is None:
                raise ProcessError("Embedding process did not exit after termination.")
        elif record:
            terminate_verified(record)
        self.proc = None
        self.record = None

    def restore(self, record):
        self.proc = spawn_exact(record)
        try:
            current = capture_process(self.proc.pid)
        except Exception as exc:
            self.stop(None)
            raise ProcessError("Embedding server could not be verified after launch.") from exc
        self.record = {**record, **current}
        return dict(self.record)
