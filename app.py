"""llama-server live monitor — local web dashboard.

Run:
    python app.py --llama-url http://localhost:8080 --llama-log .\\llama.log --port 8500

Then open http://localhost:8500

The dashboard can also launch and manage llama-server itself (browse to a
.gguf, set flags, Launch/Stop/Restart). When it launches a server it repoints
its own monitoring at it, so --llama-url / --llama-log are just the *initial*
target to watch if a server is already running.
"""

from __future__ import annotations

import argparse
import os
import string
import time
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import flags as flags_mod
import store
from collectors import (
    GpuCollector,
    LlamaCollector,
    LogTailer,
    collect_sysmem,
    find_llama_pids,
    parse_device_baseline,
    parse_device_split,
    parse_spec_enabled,
)
from launcher import LaunchError, ServerManager, resolve_binary

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
MIB = 1024 * 1024

# The fallback watch target when neither --llama-url nor $LLAMA_URL is given. A
# value different from this means the user explicitly chose a server to watch, so
# it takes precedence over re-adopting a previously-launched one (see build_app).
DEFAULT_LLAMA_URL = "http://localhost:8080"


def _seg(label: str, num_bytes: int) -> dict:
    return {"label": label, "bytes": num_bytes, "kind": "cpu" if label == "CPU" else "gpu"}


def _delta_split(baseline: dict, gpu_data: dict) -> list:
    """Estimate per-GPU model footprint = free-at-load minus free-now (NVML).

    Approximate: other processes allocating/freeing GPU memory since load can
    skew it. CPU/system-RAM is intentionally excluded — its delta is dominated
    by unrelated OS/app/disk-cache activity and is not attributable to llama.
    """
    split = []
    for d in gpu_data.get("devices", []):
        key = f"CUDA{d['index']}"
        base = baseline.get(key)
        if base is None or d.get("mem_total") is None:
            continue
        free_now_mib = (d["mem_total"] - d.get("mem_used", 0)) / MIB
        est = max(0.0, base - free_now_mib)
        split.append(_seg(key, int(est * MIB)))
    return split


def build_app(args) -> FastAPI:
    gpu = GpuCollector()

    # The monitored target lives in a mutable holder so launching a server can
    # repoint the collectors at runtime (they're rebuilt fresh, not mutated, so
    # all of LlamaCollector's cached state and the tailer's position reset
    # cleanly on a model swap). The CLI args are just the initial target.
    rt = {
        "llama": LlamaCollector(args.llama_url),
        "tailer": LogTailer(args.llama_log),
        "log_path": args.llama_log,
        "port": urlparse(args.llama_url).port,
    }
    state = {"active": False}

    # Log-derived values are re-parsed at most every 10s so a model swap /
    # server restart is picked up without re-reading the file on every poll.
    log_cache = {"ts": 0.0, "split": {}, "baseline": {}, "spec": False}

    def retarget(url: str, log_path: str, port: int) -> None:
        """Point the dashboard's collectors at a (newly launched) server."""
        old = rt["llama"]
        rt["llama"] = LlamaCollector(url)
        rt["tailer"] = LogTailer(log_path)
        rt["log_path"] = log_path
        rt["port"] = port
        state["active"] = False
        log_cache["ts"] = 0.0  # force a fresh split/spec parse from the new log
        try:
            old.close()
        except Exception:
            pass

    manager = ServerManager(retarget)

    # If a previous dashboard run launched a server that's still alive, re-adopt
    # it and point monitoring at it — so killing and relaunching the dashboard
    # reconnects to the running server instead of showing it disconnected.
    #
    # But an explicit --llama-url / $LLAMA_URL (anything other than the default)
    # is a deliberate "watch this server" instruction and wins: in that case we
    # skip adoption entirely and watch exactly what was asked for. A bare
    # `python app.py` (default URL) still auto-reconnects.
    cli_target_explicit = (args.llama_url or "") != DEFAULT_LLAMA_URL
    if not cli_target_explicit:
        adopted = manager.adopt()
        if adopted:
            a_port = adopted.get("port")
            retarget(f"http://127.0.0.1:{a_port}",
                     adopted.get("log_path") or store.MANAGED_LOG, a_port)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup happens above (collector construction); only teardown is needed.
        # A launched llama-server is intentionally left running — the user stops
        # it explicitly from the UI.
        try:
            yield
        finally:
            rt["llama"].close()
            gpu.shutdown()

    app = FastAPI(title="llama-monitor", lifespan=lifespan)

    def from_log() -> dict:
        now = time.time()
        log_path = rt["log_path"]
        if log_path and now - log_cache["ts"] > 10:
            log_cache["split"] = parse_device_split(log_path)
            log_cache["baseline"] = parse_device_baseline(log_path)
            log_cache["spec"] = parse_spec_enabled(log_path)
            log_cache["ts"] = now
        return log_cache

    def collect_gated(lite: int):
        """Choose how hard to poll llama-server.

        With a log to watch: stay at level "none" (no HTTP, no wake) until the
        log shows activity, then poll "full" until /slots confirms it's idle
        again. Without a log: the old HTTP adaptive behaviour (frontend lite).
        """
        llama = rt["llama"]
        tailer = rt["tailer"]
        if tailer.ok:
            saw = tailer.poll()  # True / False / None
            if saw:
                state["active"] = True
            data = llama.collect("full" if state["active"] else "none")
            if state["active"] and not saw:
                if ((data.get("slots") or {}).get("busy") or 0) == 0:
                    state["active"] = False  # request finished
            data["log_mode"] = True
        else:
            data = llama.collect("slots" if lite else "full")
            busy = (data.get("slots") or {}).get("busy") or 0
            proc = (data.get("requests") or {}).get("processing") or 0
            state["active"] = bool(busy or proc)
            data["log_mode"] = False
        data["active"] = state["active"]

        # Merge exact per-request stats parsed from the log (preferred over the
        # HTTP gauges/approximations) when available.
        L = tailer.last
        if L.get("prefill"):
            data["prefill_last"] = L["prefill"]
        if L.get("decode") and data.get("throughput"):
            data["throughput"]["decode_tps_avg"] = L["decode"]["tps"]
        # Exact timing breakdown of the most recent completed request:
        # prompt-processing (pp) + generation = total, straight from the log's
        # print_timing block. (Generation is reasoning + answer combined — the
        # server doesn't time those separately.)
        last_req = {}
        if L.get("prefill"):
            last_req["pp"] = L["prefill"]
        if L.get("decode"):
            last_req["generation"] = L["decode"]
        if L.get("total"):
            last_req["total"] = L["total"]
        if last_req:
            data["last_request"] = last_req
        if L.get("draft"):
            sp = data.setdefault("spec", {})
            sp["enabled"] = True
            sp["accept_rate"] = L["draft"].get("rate")
            sp["mean_len"] = L["draft"].get("mean_len")
            sp["accepted"] = L["draft"].get("accepted")
            sp["generated"] = L["draft"].get("generated")
        return data

    @app.get("/api/stats")
    def stats(lite: int = 0) -> JSONResponse:
        llama_pids = find_llama_pids(port=rt["port"])
        data = collect_gated(lite)
        gpu_data = gpu.collect(llama_pids)
        data["gpu"] = gpu_data
        data["sysmem"] = collect_sysmem(llama_pids)

        # Build the memory-split view, best source first:
        #   1. NVML per-process VRAM       — exact (Linux / TCC drivers only)
        #   2. log buffer-size lines       — exact (builds that print them)
        #   3. NVML delta vs log baseline  — live approximation (Windows/WDDM)
        split = []
        source = None
        log_path = rt["log_path"]
        nvml_split = [
            {"label": f"CUDA{d['index']}", "bytes": d["llama_mem"], "kind": "gpu"}
            for d in gpu_data.get("devices", [])
            if d.get("llama_mem")
        ]
        if nvml_split:
            split, source = nvml_split, "nvml"
        elif log_path:
            cache = from_log()
            if cache["split"]:
                for label, mib in cache["split"].items():
                    split.append(_seg(label, int(mib * MIB)))
                source = "log"
            elif cache["baseline"]:
                split, source = _delta_split(cache["baseline"], gpu_data), "nvml-delta"

        data["split"] = split
        data["split_source"] = source
        data["split_log_configured"] = bool(log_path)

        # Idle-time MTP detection from the log complements the collector's latch
        # (which can only fire once a request has actually used speculation).
        if log_path and not (data.get("spec") or {}).get("enabled"):
            if from_log()["spec"]:
                data.setdefault("spec", {})["enabled"] = True

        return JSONResponse(data)

    # ----------------------------------------------------------------------- #
    # Launcher / configuration API                                            #
    # ----------------------------------------------------------------------- #

    def launcher_state() -> dict:
        settings = store.get_settings()
        return {
            "settings": settings,
            "binary_valid": bool(resolve_binary(settings.get("llama_server_path"))),
            "configs": store.list_configs(),
            "status": manager.status(),
            "managed_log": store.MANAGED_LOG,
        }

    @app.get("/api/launcher/state")
    def get_launcher_state() -> JSONResponse:
        return JSONResponse(launcher_state())

    @app.get("/api/launcher/flags")
    def get_flags() -> JSONResponse:
        """The flags supported by the installed llama-server (for the dropdown
        and per-flag descriptions), or a bundled fallback if it can't be run."""
        binary = resolve_binary(store.get_settings().get("llama_server_path"))
        return JSONResponse(flags_mod.get_server_flags(binary))

    # Tail at most the last ~256 KB when the console is first opened, so we don't
    # ship a huge file on the initial fetch.
    CONSOLE_HEAD = 256 * 1024

    @app.get("/api/launcher/console")
    def get_console(offset: int = 0) -> JSONResponse:
        """Stream the active server log (console output) incrementally.

        Reads the currently-monitored log (the managed log for launched servers,
        or the watched ``--llama-log`` for an external one). Mirrors the tailer's
        truncation handling so a restart/rotation re-reads from the top.
        """
        path = rt["log_path"] or store.MANAGED_LOG
        if not path or not os.path.isfile(path):
            return JSONResponse({"available": False, "content": "", "offset": 0, "size": 0, "path": path})
        try:
            size = os.path.getsize(path)
            start = offset
            if start > size or start < 0:      # truncated / rotated -> from top
                start = 0
            if start == 0 and size > CONSOLE_HEAD:
                start = size - CONSOLE_HEAD
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(start)
                content = f.read()
                new_offset = f.tell()
        except Exception as e:
            return JSONResponse({"available": False, "error": str(e), "content": "",
                                 "offset": offset, "size": 0, "path": path})
        return JSONResponse({"available": True, "content": content,
                             "offset": new_offset, "size": size, "path": path})

    @app.post("/api/launcher/settings")
    async def post_settings(request: Request) -> JSONResponse:
        body = await request.json()
        store.update_settings(
            llama_server_path=body.get("llama_server_path"),
            models_dir=body.get("models_dir"),
            default_port=body.get("default_port"),
        )
        return JSONResponse(launcher_state())

    @app.get("/api/browse")
    def browse(path: str = "", ext: str = "") -> JSONResponse:
        """List subdirectories and (optionally extension-filtered) files.

        Localhost-only by default (the dashboard binds to 127.0.0.1); this does
        expose directory listings to anything that can reach it.
        """
        exts = [e.strip().lower() for e in ext.split(",") if e.strip()]

        # Empty path -> the drive list on Windows, home dir elsewhere.
        if not path:
            if os.name == "nt":
                drives = [f"{d}:\\" for d in string.ascii_uppercase
                          if os.path.exists(f"{d}:\\")]
                return JSONResponse({"path": "", "parent": None, "dirs": drives, "files": []})
            path = os.path.expanduser("~")

        path = os.path.abspath(path)
        try:
            entries = os.listdir(path)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        dirs, files = [], []
        for name in sorted(entries, key=str.lower):
            full = os.path.join(path, name)
            try:
                if os.path.isdir(full):
                    dirs.append(full)
                elif os.path.isfile(full):
                    if not exts or os.path.splitext(name)[1].lower() in exts:
                        files.append(full)
            except Exception:
                continue

        parent = os.path.dirname(path)
        if parent == path:   # at a drive/filesystem root -> step up to drive list
            parent = ""
        return JSONResponse({"path": path, "parent": parent, "dirs": dirs, "files": files})

    @app.get("/api/configs")
    def get_configs() -> JSONResponse:
        return JSONResponse({"configs": store.list_configs()})

    @app.post("/api/configs")
    async def post_config(request: Request) -> JSONResponse:
        body = await request.json()
        try:
            configs = store.upsert_config(body)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"configs": configs})

    @app.delete("/api/configs/{name}")
    def remove_config(name: str) -> JSONResponse:
        return JSONResponse({"configs": store.delete_config(name)})

    @app.post("/api/configs/default")
    async def post_default_config(request: Request) -> JSONResponse:
        """Set (or clear, with an empty name) the config that auto-loads on open
        when no server is running. Returns the full launcher state."""
        body = await request.json()
        try:
            store.set_default_config(body.get("name"))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(launcher_state())

    @app.post("/api/launcher/launch")
    async def post_launch(request: Request) -> JSONResponse:
        body = await request.json()
        try:
            manager.launch(body)
        except LaunchError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(launcher_state())

    @app.post("/api/launcher/stop")
    def post_stop() -> JSONResponse:
        manager.stop()
        return JSONResponse(launcher_state())

    @app.post("/api/launcher/restart")
    def post_restart() -> JSONResponse:
        try:
            manager.restart()
        except LaunchError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(launcher_state())

    @app.get("/")
    def index() -> FileResponse:
        # no-store so the browser always loads the current JS (otherwise a stale
        # cached page keeps the old polling behaviour after an upgrade).
        return FileResponse(
            os.path.join(STATIC_DIR, "index.html"),
            headers={"Cache-Control": "no-store"},
        )

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app


def main() -> None:
    p = argparse.ArgumentParser(description="llama-server live monitor")
    p.add_argument(
        "--llama-url",
        default=os.environ.get("LLAMA_URL", DEFAULT_LLAMA_URL),
        help=f"Base URL of a running llama-server to watch (default: {DEFAULT_LLAMA_URL}). "
             "A non-default value takes precedence over re-adopting a launched server.",
    )
    p.add_argument(
        "--llama-log",
        default=os.environ.get("LLAMA_LOG"),
        help="Path to a running llama-server's startup log (for the CPU split number)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MONITOR_PORT", "8500")),
        help="Port for this dashboard (default: 8500)",
    )
    p.add_argument("--host", default="127.0.0.1")
    args = p.parse_args()

    app = build_app(args)
    print(f"llama-monitor -> dashboard on http://{args.host}:{args.port}")
    print(f"   initial llama-server target: {args.llama_url}")
    if args.llama_log:
        print(f"   reading CPU split from {args.llama_log}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
