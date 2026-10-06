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
import hmac
import ipaddress
import json
import secrets
import logging
import logging.config
import string
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
    collect_sysmem,
    find_llama_pids,
)
from history import HistoryDB, LogFollower, decode_log_line
from launcher import LaunchError, ServerManager, resolve_binary
from gaming import GamingCoordinator
from apollo import ApolloClient, certificate_fingerprint, parse_url, protect, unprotect
from apollo_setup import commands as hook_commands, write_hook_config
from processes import discover_registered
from instance import backend_lock
from windows_startup import WindowsStartup
from background import log_config as background_log_config

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
    # all of LlamaCollector's cached state resets on a model swap.
    db = HistoryDB(os.path.join(store.HOME_DIR, "history.sqlite"))
    follower = LogFollower(db, args.llama_log,
                           prompt_dir=getattr(args, "llama_prompts_dir", None))
    rt = {
        "llama": LlamaCollector(args.llama_url),
        "log_path": args.llama_log,
        "port": urlparse(args.llama_url).port,
    }
    state = {"active": False, "activity_seq": follower.snapshot()["activity_seq"]}

    def managed_prompt_dir(config: dict) -> str | None:
        if config.get("log_prompts"):
            return store.PROMPTS_DIR
        for entry in config.get("flags") or []:
            if entry.get("enabled") is not False and entry.get("flag") == "--log-prompts-dir":
                return entry.get("value") or None
        return None

    def retarget(url: str, log_path: str, port: int) -> None:
        """Point the dashboard's collectors at a (newly launched) server."""
        old = rt["llama"]
        rt["llama"] = LlamaCollector(url)
        record = store.get_running()
        follower.attach(log_path, run_id=(record or {}).get("run_id"), managed=True,
                        started_at=(record or {}).get("started_at"),
                        prompt_dir=managed_prompt_dir((record or {}).get("config") or {}))
        rt["log_path"] = log_path
        rt["port"] = port
        state["active"] = False
        state["activity_seq"] = follower.snapshot()["activity_seq"]
        try:
            old.close()
        except Exception:
            pass

    manager = ServerManager(retarget, finish_log=follower.finish_segment)

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

    gaming = GamingCoordinator(manager)
    windows_startup = WindowsStartup(gaming, getattr(args, "port", 8500))
    gaming.startup_results = windows_startup.consume_result

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        follower.start()
        gaming.start()
        # A launched llama-server is intentionally left running — the user stops
        # it explicitly from the UI.
        try:
            yield
        finally:
            gaming.close()
            follower.stop()
            rt["llama"].close()
            gpu.shutdown()

    app = FastAPI(title="llama-monitor", lifespan=lifespan)

    def local_request(request):
        try:
            local = ipaddress.ip_address(request.client.host).is_loopback
        except (ValueError, AttributeError):
            local = False
        origin = request.headers.get("origin")
        if not local or (origin and origin.rstrip("/") != str(request.base_url).rstrip("/")):
            raise LaunchError("Gaming controls are available only from the local dashboard.")

    def hook_request(request):
        local_request(request)
        encrypted = store.load_state()["gaming"].get("hook_token")
        if not encrypted or not hmac.compare_digest(
                request.headers.get("x-llama-monitor-token", ""), unprotect(encrypted)):
            raise LaunchError("Invalid integration token.")

    def gaming_error(exc, code=409):
        message = str(exc) if isinstance(exc, (ValueError, LaunchError)) else "Gaming integration operation failed."
        return JSONResponse({"error": message}, status_code=code)

    @app.get("/api/startup/state")
    def startup_state():
        return JSONResponse(windows_startup.state())

    @app.post("/api/startup/install")
    def startup_install(request: Request, body: dict):
        try:
            local_request(request)
            if cli_target_explicit:
                raise ValueError("Restart with plain python app.py before configuring startup.")
            return JSONResponse(windows_startup.setup(mode=body.get("mode", "boot"),
                autostart_models=body.get("autostart_models", True)), status_code=202)
        except Exception as exc:
            return gaming_error(exc, 400)

    @app.post("/api/startup/remove")
    def startup_remove(request: Request):
        try:
            local_request(request)
            return JSONResponse(windows_startup.setup(action="remove", autostart_models=False), status_code=202)
        except Exception as exc:
            return gaming_error(exc, 400)

    @app.get("/api/gaming/state")
    def gaming_state():
        return JSONResponse({**gaming.state(), "commands": hook_commands()})

    @app.post("/api/gaming/settings")
    def gaming_settings(request: Request, body: dict):
        try:
            local_request(request)
            with gaming.lock:
                if gaming.transition:
                    raise LaunchError("Finish or cancel the pending transition before changing integration settings.")
                settings = store.load_state()["gaming"]
                changes = {}
                if "apollo_url" in body:
                    parse_url(body["apollo_url"])
                    changes["apollo_url"] = body["apollo_url"]
                    if body["apollo_url"] != settings["apollo_url"]:
                        changes["certificate_sha256"] = None
                if body.get("password"):
                    if not body.get("username") or ":" in body["username"]:
                        raise ValueError("Enter a valid Apollo username.")
                    changes["credentials"] = protect(json.dumps({"username": body["username"], "password": body["password"]}))
                    changes["certificate_sha256"] = None
                if "enabled" in body:
                    if not isinstance(body["enabled"], bool):
                        raise ValueError("enabled must be a boolean.")
                    if body["enabled"]:
                        if cli_target_explicit:
                            raise LaunchError("Restart llama-monitor with plain python app.py before enabling integration.")
                        if not settings.get("credentials") or not settings.get("certificate_sha256"):
                            raise ValueError("Save credentials and test Apollo before enabling integration.")
                        gaming.apollo.connected(settings)
                    changes["enabled"] = body["enabled"]
                encrypted = settings.get("hook_token") or protect(secrets.token_urlsafe(32))
                changes["hook_token"] = encrypted
                port = getattr(args, "port", 8500)
                write_hook_config(encrypted, port)
                store.update_gaming(**changes)
            return gaming_state()
        except Exception as exc:
            return gaming_error(exc, 400)

    @app.post("/api/gaming/test")
    def gaming_test(request: Request):
        try:
            local_request(request)
            with gaming.lock:
                settings = store.load_state()["gaming"]
                fingerprint = certificate_fingerprint(settings["apollo_url"])
                candidate = {**settings, "certificate_sha256": fingerprint}
                clients = gaming.apollo.connected(candidate)
                store.update_gaming(certificate_sha256=fingerprint)
                gaming.connected = clients
                gaming.reconciled = True
                gaming.integration_error = None
            return gaming_state()
        except Exception as exc:
            return gaming_error(exc, 400)

    @app.post("/api/gaming/auxiliary")
    def gaming_auxiliary(request: Request, body: dict):
        try:
            local_request(request)
            with gaming.lock:
                if gaming.transition:
                    raise LaunchError("Cannot change registered servers during a transition.")
                port = int(body.get("port", 8081))
                if not 1 <= port <= 65535 or port == (manager.current or {}).get("port", store.DEFAULT_PORT):
                    raise ValueError("Choose a valid port different from the managed model.")
                binary = resolve_binary(store.get_settings().get("llama_server_path"))
                if not binary:
                    raise ValueError("Set the llama-server executable first.")
                record = discover_registered(binary, port)
                if not record:
                    raise ValueError("No running llama-server matches this executable and embedding port.")
                store.update_gaming(auxiliary={"executable": record["executable"], "port": port})
            return gaming_state()
        except Exception as exc:
            return gaming_error(exc, 400)

    @app.post("/api/gaming/prepare")
    def gaming_prepare(request: Request):
        try:
            hook_request(request)
            if not store.load_state()["gaming"]["enabled"]:
                return JSONResponse({"enabled": False, "skipped": True})
            return JSONResponse(gaming.prepare())
        except Exception as exc:
            return gaming_error(exc)

    @app.post("/api/gaming/session-ended")
    def gaming_ended(request: Request):
        try:
            hook_request(request)
            return JSONResponse(gaming.session_ended(), status_code=202)
        except Exception as exc:
            return gaming_error(exc)

    @app.post("/api/gaming/retry")
    def gaming_retry(request: Request):
        try:
            local_request(request)
            return JSONResponse(gaming.retry())
        except Exception as exc:
            return gaming_error(exc)

    @app.post("/api/gaming/cancel")
    def gaming_cancel(request: Request):
        try:
            local_request(request)
            return JSONResponse(gaming.cancel())
        except Exception as exc:
            return gaming_error(exc)

    @app.post("/api/gaming/servers/{server_id}/stop")
    def gaming_stop_server(server_id: str, request: Request):
        try:
            local_request(request)
            return JSONResponse(gaming.stop_server(server_id))
        except Exception as exc:
            return gaming_error(exc)

    def collect_gated(lite: int):
        """Choose how hard to poll llama-server.

        With a log to watch: stay at level "none" (no HTTP, no wake) until the
        log shows activity, then poll "full" until /slots confirms it's idle
        again. Without a log: the old HTTP adaptive behaviour (frontend lite).
        """
        llama = rt["llama"]
        snapshot = follower.snapshot()
        if snapshot["available"]:
            saw = snapshot["activity_seq"] != state["activity_seq"]
            state["activity_seq"] = snapshot["activity_seq"]
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

        data["log_status"] = {k: snapshot[k] for k in ("available", "configured", "gap", "error")}
        follower.set_model((data.get("model") or {}).get("name"))
        last = db.latest_complete(snapshot["run_id"])
        if last:
            def timing(tokens, seconds, *, decode=False):
                # llama-server counts the first generated token in the total
                # but excludes it from its printed eval tokens/second rate.
                rate_tokens = max(0, tokens - 1) if decode and tokens is not None else tokens
                return {"tokens": tokens, "secs": seconds,
                        "tps": rate_tokens / seconds if rate_tokens is not None
                        and seconds and seconds > 0 else None}
            pp = timing(last["prompt_tokens"], last["prompt_seconds"])
            dec = timing(last["generated_tokens"], last["decode_seconds"], decode=True)
            total_tokens = (last["prompt_tokens"] or 0) + (last["generated_tokens"] or 0)
            total = timing(total_tokens, last["total_seconds"])
            data["prefill_last"] = pp
            data["last_request"] = {"pp": pp, "generation": dec, "total": total}
        if last and last.get("draft_generated") is not None:
            sp = data.setdefault("spec", {})
            sp["enabled"] = True
            sp["accept_rate"] = last["draft_accept_rate"]
            sp["mean_len"] = last["draft_mean_len"]
            sp["accepted"] = last["draft_accepted"]
            sp["generated"] = last["draft_generated"]
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
            cache = follower.snapshot()
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
            if follower.snapshot()["spec"]:
                data.setdefault("spec", {})["enabled"] = True

        gaming_status = gaming.state()
        data["gaming"] = gaming_status
        primary = next((s for s in gaming_status["servers"] if s["id"] == "primary"), None)
        if gaming_status["blocked"] and (not primary or primary["status"] != "ready"):
            for key in ("model", "slots", "requests", "kv", "throughput", "spec", "last_request", "prefill_last"):
                data.pop(key, None)
            data.update(online=False, active=False, split=[], split_source=None)

        return JSONResponse(data)

    @app.get("/api/history")
    def get_history(model: str = "", state: str = "", from_ts: float | None = None,
                    to_ts: float | None = None, sort: str = "time", order: str = "desc",
                    cursor: str | None = None, limit: int = 50) -> JSONResponse:
        try:
            result = db.list(model=model, state=state, from_ts=from_ts, to_ts=to_ts,
                             sort=sort, order=order, cursor=cursor, limit=limit)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        result["database_path"] = db.path
        result["log_status"] = {k: follower.snapshot()[k] for k in
                                ("available", "configured", "gap", "error", "prompts_configured")}
        return JSONResponse(result)

    @app.delete("/api/history")
    def delete_history() -> JSONResponse:
        return JSONResponse({"deleted": follower.clear_history()})

    @app.get("/api/history/{activity_id}/prompt")
    def get_history_prompt(activity_id: str) -> JSONResponse:
        prompt = db.get_prompt(activity_id)
        if prompt is None:
            return JSONResponse({"error": "Prompt unavailable"}, status_code=404)
        return JSONResponse(prompt)

    # ----------------------------------------------------------------------- #
    # Launcher / configuration API                                            #
    # ----------------------------------------------------------------------- #

    def launcher_state() -> dict:
        settings = store.get_settings()
        status = manager.status()
        pending_config = gaming.primary_config()
        if pending_config:
            status = {**status, "config": pending_config, "config_name": pending_config.get("name")}
        return {
            "settings": settings,
            "binary_valid": bool(resolve_binary(settings.get("llama_server_path"))),
            "configs": store.list_configs(),
            "status": status,
            "managed_log": store.MANAGED_LOG,
            "gaming": gaming.state(),
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
        or the watched ``--llama-log`` for an external one). Mirrors the follower's
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
            with open(path, "rb") as f:
                f.seek(start)
                if start and offset == 0:
                    f.readline()  # drop a partial first line after a bounded tail
                first_complete = f.tell()
                raw = f.read()
                end = raw.rfind(b"\n")
                if end < 0:
                    raw = b""
                    new_offset = first_complete
                else:
                    raw = raw[:end + 1]
                    new_offset = first_complete + end + 1
            content = "\n".join(decode_log_line(line.decode("utf-8", "replace")) or
                                line.decode("utf-8", "replace").strip()
                                for line in raw.splitlines())
            if raw.endswith(b"\n") and content:
                content += "\n"
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
            gaming.launch(body)
        except LaunchError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(launcher_state())

    @app.post("/api/launcher/stop")
    def post_stop() -> JSONResponse:
        try:
            gaming.stop_server()
        except Exception as exc:
            return gaming_error(exc)
        return JSONResponse(launcher_state())

    @app.post("/api/launcher/restart")
    def post_restart() -> JSONResponse:
        try:
            gaming.launch()
        except LaunchError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(launcher_state())

    @app.get("/")
    def index() -> FileResponse:
        # no-store so the browser always loads the current JS (otherwise a stale
        # cached page keeps the old polling behaviour after an upgrade).
        return FileResponse(
            os.path.join(STATIC_DIR, "app", "index.html"),
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
        "--llama-prompts-dir",
        default=os.environ.get("LLAMA_PROMPTS_DIR"),
        help="Directory written by an external server's --log-prompts-dir flag",
    )
    p.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MONITOR_PORT", "8500")),
        help="Port for this dashboard (default: 8500)",
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--background", action="store_true", help="Run without a console and log to ~/.llama-monitor/backend.log")
    args = p.parse_args()

    logging_config = background_log_config(store.HOME_DIR) if args.background else None
    if logging_config:
        logging.config.dictConfig(logging_config)

    try:
        with backend_lock(store.HOME_DIR):
            app = build_app(args)
            if args.background:
                logging.getLogger("llama_monitor").info("Backend starting on port %s", args.port)
                uvicorn.run(app, host=args.host, port=args.port, log_level="warning", log_config=logging_config)
            else:
                print(f"llama-monitor -> dashboard on http://{args.host}:{args.port}")
                print(f"   initial llama-server target: {args.llama_url}")
                if args.llama_log:
                    print(f"   reading CPU split from {args.llama_log}")
                uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    except RuntimeError as exc:
        if args.background:
            logging.getLogger("llama_monitor").error("Backend stopped: %s", exc)
            raise SystemExit(1)
        p.exit(1, f"{exc}\n")
    except Exception:
        if args.background:
            logging.getLogger("llama_monitor").exception("Backend failed")
            raise SystemExit(1)
        raise


if __name__ == "__main__":
    main()
