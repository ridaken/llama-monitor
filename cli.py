"""Command-line client for a running llama-monitor backend (native or Docker)."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlparse, quote

import httpx


class CLIError(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


class ConnectionFailure(CLIError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CLIError(message, 2)


def positive(value):
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return number


def parser():
    common = Parser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--url", default=argparse.SUPPRESS, help="Backend URL (or LLAMA_MONITOR_URL)")
    common.add_argument("--request-timeout", type=positive, default=argparse.SUPPRESS)
    root = Parser(description=__doc__, parents=[common])
    commands = root.add_subparsers(dest="command", required=True, parser_class=Parser)
    for name in ("status", "stop", "stats", "doctor"):
        commands.add_parser(name, parents=[common])
    for name in ("launch", "restart"):
        command = commands.add_parser(name, parents=[common])
        command.add_argument("--wait", action="store_true", help="Wait for this launch to become ready")
        command.add_argument("--timeout", type=positive, default=300)
        if name == "launch":
            source = command.add_mutually_exclusive_group(required=True)
            source.add_argument("--config", metavar="NAME")
            source.add_argument("--config-file", metavar="PATH", help="Config JSON; - reads stdin")
    wait = commands.add_parser("wait", parents=[common])
    condition = wait.add_mutually_exclusive_group(required=True)
    condition.add_argument("--backend", action="store_true")
    condition.add_argument("--ready", action="store_true")
    wait.add_argument("--timeout", type=positive, default=300)
    logs = commands.add_parser("logs", parents=[common])
    logs.add_argument("--tail", type=int, default=100)
    smoke = commands.add_parser("smoke", parents=[common])
    smoke.add_argument("--timeout", type=positive, default=60, help="Overall probe deadline, at most 300 seconds")
    proxy = commands.add_parser("proxy", parents=[common]).add_subparsers(dest="action", required=True, parser_class=Parser)
    proxy.add_parser("status", parents=[common])
    settings = proxy.add_parser("settings", parents=[common])
    for option in ("enabled", "log-prompts", "log-responses"):
        settings.add_argument(f"--{option}", choices=("on", "off"))
    probe = proxy.add_parser("smoke", parents=[common])
    probe.add_argument("--stream", action="store_true")
    probe.add_argument("--timeout", type=positive, default=60)
    probe.add_argument("--api-key-env", metavar="NAME", help="Read the upstream API key from this environment variable")
    requests = commands.add_parser("requests", parents=[common]).add_subparsers(dest="action", required=True, parser_class=Parser)
    listing = requests.add_parser("list", parents=[common])
    for option in ("model", "endpoint", "state", "cursor"):
        listing.add_argument(f"--{option}")
    for option in ("from-ts", "to-ts"):
        listing.add_argument(f"--{option}", type=float)
    listing.add_argument("--limit", type=int, default=50)
    requests.add_parser("show", parents=[common]).add_argument("id")
    requests.add_parser("clear", parents=[common]).add_argument("--yes", action="store_true")
    for name in ("configs", "history", "gaming", "startup"):
        group = commands.add_parser(name, parents=[common])
        subs = group.add_subparsers(dest="action", required=True, parser_class=Parser)
        if name == "configs":
            subs.add_parser("list", parents=[common])
            subs.add_parser("show", parents=[common]).add_argument("name")
        elif name == "history":
            history = subs.add_parser("list", parents=[common])
            for flag in ("model", "state", "cursor"):
                history.add_argument(f"--{flag}")
            for flag in ("from-ts", "to-ts"):
                history.add_argument(f"--{flag}", type=float)
            history.add_argument("--sort", choices=("time", "duration", "prompt_tokens", "generated_tokens"))
            history.add_argument("--order", choices=("asc", "desc"))
            history.add_argument("--limit", type=int, default=50)
        else:
            subs.add_parser("status", parents=[common])
    return root


class Client:
    def __init__(self, url, timeout):
        self.http = httpx.Client(base_url=url.rstrip("/"), timeout=timeout, trust_env=False, follow_redirects=False)

    def request(self, method, path, **kwargs):
        try:
            response = self.http.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise ConnectionFailure("Backend request timed out; a submitted operation may still complete.", 4) from exc
        except httpx.HTTPError as exc:
            raise ConnectionFailure("Cannot connect to the llama-monitor backend.", 3) from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise CLIError("Backend returned invalid JSON.", 3) from exc
        if not isinstance(body, dict):
            raise CLIError("Backend returned an unexpected response.", 3)
        if not response.is_success:
            raise CLIError(str(body.get("error") or body.get("detail") or f"HTTP {response.status_code}"),
                           4 if response.status_code == 504 else 1)
        return body


def wait_for(client, *, backend=False, timeout=300, run_id=None):
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CLIError("Timed out waiting for backend availability." if backend else "Timed out waiting for model readiness.", 4)
        try:
            result = client.request("GET", "/api/health" if backend else "/api/launcher/readiness",
                                    timeout=min(client.http.timeout.read, remaining))
        except CLIError as exc:
            if not backend or not isinstance(exc, ConnectionFailure):
                raise
        else:
            if time.monotonic() >= deadline:
                raise CLIError("Verification deadline exceeded.", 4)
            if backend:
                if result.get("status") != "ok":
                    raise CLIError("Unexpected backend health response.", 3)
                return result
            if "ready" not in result or "state" not in result:
                raise CLIError("Unexpected readiness response.", 3)
            if result["state"] in ("stopped", "failed", "blocked"):
                raise CLIError(result.get("reason", "Managed model is unavailable."))
            if not result.get("run_id"):
                raise CLIError("Readiness response has no managed run identity.", 3)
            run_id = run_id or result["run_id"]
            if run_id != result["run_id"]:
                raise CLIError("Managed server changed while waiting.")
            if result["ready"]:
                return result
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))


def configs(client, name=None):
    body = client.request("GET", "/api/configs")
    if not isinstance(body.get("configs"), list):
        raise CLIError("Unexpected configurations response.", 3)
    if name is None:
        return body
    for config in body["configs"]:
        if isinstance(config, dict) and config.get("name") == name:
            return config
    raise CLIError(f"No saved configuration named {name!r}.")


def execute(args, client):
    command = args.command
    if command == "proxy":
        if args.action == "status":
            return client.request("GET", "/api/proxy/state")
        if args.action == "settings":
            changes = {key: getattr(args, key) == "on" for key in ("enabled", "log_prompts", "log_responses")
                       if getattr(args, key) is not None}
            return client.request("POST", "/api/proxy/settings", json=changes) if changes else client.request("GET", "/api/proxy/state")
        return proxy_smoke(args, client)
    if command == "requests":
        if args.action == "show":
            return client.request("GET", f"/api/requests/{quote(args.id, safe='')}")
        if args.action == "clear":
            if not args.yes:
                raise CLIError("Clearing request history requires --yes.", 2)
            return client.request("DELETE", "/api/requests")
        if not 1 <= args.limit <= 200:
            raise CLIError("--limit must be between 1 and 200.", 2)
        params = {key: getattr(args, key) for key in ("model", "endpoint", "state", "cursor", "from_ts", "to_ts", "limit")
                  if getattr(args, key) is not None}
        return client.request("GET", "/api/requests", params=params)
    if command == "doctor":
        return client.request("GET", "/api/launcher/doctor")
    if command == "status":
        health = client.request("GET", "/api/health")
        if health.get("status") != "ok":
            raise CLIError("Unexpected backend health response.", 3)
        return {"backend": health, "launcher": client.request("GET", "/api/launcher/state"),
                "readiness": client.request("GET", "/api/launcher/readiness")}
    if command == "configs":
        return configs(client, args.name if args.action == "show" else None)
    if command in ("launch", "restart", "stop"):
        body = {}
        if command == "launch":
            if args.config:
                body = configs(client, args.config)
            else:
                try:
                    body = json.loads(sys.stdin.read() if args.config_file == "-" else
                                      Path(args.config_file).read_text(encoding="utf-8-sig"))
                except (OSError, ValueError) as exc:
                    raise CLIError("Cannot read configuration JSON.", 2) from exc
                if not isinstance(body, dict):
                    raise CLIError("Configuration JSON must be an object.", 2)
        result = client.request("POST", f"/api/launcher/{command}", json=body)
        if command != "stop" and args.wait:
            run_id = result.get("status", {}).get("run_id")
            if not run_id:
                raise CLIError("Launch response has no managed run identity.", 3)
            result["readiness"] = wait_for(client, timeout=args.timeout, run_id=run_id)
        return result
    if command == "wait":
        return wait_for(client, backend=args.backend, timeout=args.timeout)
    if command == "stats":
        return client.request("GET", "/api/stats")
    if command == "logs":
        if args.tail <= 0:
            raise CLIError("--tail must be positive.", 2)
        result = client.request("GET", "/api/launcher/console")
        if not result.get("available"):
            raise CLIError(result.get("error") or "Server log is unavailable.")
        result["content"] = "\n".join(result.get("content", "").splitlines()[-args.tail:])
        return result
    if command == "history":
        params = {key: getattr(args, key) for key in
                  ("model", "state", "cursor", "from_ts", "to_ts", "sort", "order", "limit")
                  if getattr(args, key) is not None}
        return client.request("GET", "/api/history", params=params)
    if command in ("gaming", "startup"):
        return client.request("GET", f"/api/{command}/state")
    if command == "smoke":
        if args.timeout > 300:
            raise CLIError("Smoke timeout must be at most 300 seconds.", 2)
        return client.request("POST", "/api/launcher/smoke", json={"timeout": args.timeout}, timeout=args.timeout + 2)
    raise CLIError("Unknown command.", 2)


def proxy_smoke(args, client):
    from proxy_capture import ResponseCapture
    if args.timeout > 300:
        raise CLIError("Smoke timeout must be at most 300 seconds.", 2)
    headers = {}
    if args.api_key_env:
        key = os.environ.get(args.api_key_env)
        if not key:
            raise CLIError("The API key environment variable is empty or missing.", 2)
        headers["Authorization"] = "Bearer " + key
    state = client.request("GET", "/api/proxy/state")
    if not state.get("enabled") or not state.get("listening"):
        raise CLIError(state.get("listener_error") or "Proxy is disabled or not listening.")
    # Reach a remote proxy through the same host as its management backend.
    # Published Docker ports can differ, so allow an explicit URL override.
    base = os.environ.get("LLAMA_MONITOR_PROXY_URL")
    if not base:
        address = client.http.base_url.copy_with(scheme="http", port=state["port"], path="/")
        base = str(address).rstrip("/")
    started = time.monotonic()
    captured = ResponseCapture(True, started)
    first_chunk = None
    async def probe():
        nonlocal first_chunk
        async with httpx.AsyncClient(timeout=args.timeout, trust_env=False, follow_redirects=False) as inference:
            async with inference.stream("POST", base.rstrip("/") + "/v1/chat/completions", headers=headers, json={
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "temperature": 0, "max_tokens": 16, "stream": args.stream}) as response:
                if response.status_code >= 400:
                    raise CLIError(f"Proxy inference returned HTTP {response.status_code}.", 4 if response.status_code == 504 else 1)
                identifier = response.headers.get("x-llama-monitor-request-id")
                captured.sse = "text/event-stream" in response.headers.get("content-type", "")
                if captured.sse != args.stream:
                    raise CLIError("Proxy returned an unexpected streaming format.", 3)
                async for chunk in response.aiter_bytes():
                    if first_chunk is None:
                        first_chunk = time.monotonic() - started
                    captured.feed(chunk)
                return identifier

    async def timed_probe():
        return await asyncio.wait_for(probe(), timeout=args.timeout)

    try:
        identifier = asyncio.run(timed_probe())
        text = captured.finish()
        if time.monotonic() - started >= args.timeout:
            raise CLIError("Proxy inference verification timed out.", 4)
        if not identifier or not captured.done or captured.invalid or captured.failed or captured.first_token is None:
            raise CLIError("Proxy inference returned no generated content or an incomplete response.")
        return {"request_id": identifier, "stream": args.stream, "first_chunk_seconds": first_chunk,
                "elapsed_seconds": time.monotonic() - started, "response": json.loads(text)}
    except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
        raise CLIError("Proxy inference timed out; the upstream may still finish.", 4) from exc
    except httpx.HTTPError as exc:
        raise CLIError("Cannot connect to the proxy or read its response.", 3) from exc


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    json_output = "--json" in argv
    try:
        args = parser().parse_args(argv)
        json_output = getattr(args, "json", False)
        url = getattr(args, "url", os.environ.get("LLAMA_MONITOR_URL", "http://127.0.0.1:8500"))
        try:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query or parsed.fragment or parsed.username:
                raise ValueError("invalid backend URL")
            parsed.port  # Validate the port before constructing the HTTP client.
            client = Client(url, getattr(args, "request_timeout", 10))
        except (ValueError, httpx.InvalidURL) as exc:
            raise CLIError("--url must be a valid HTTP(S) backend URL without credentials, query, or fragment.", 2) from exc
        try:
            data = execute(args, client)
        finally:
            client.http.close()
        result, code = {"ok": True, "data": data}, 0
        if args.command == "doctor" and not data["healthy"]:
            code = 1
            result.update(ok=False, error={"message": data["summary"], "code": code})
    except CLIError as exc:
        code = exc.code
        result = {"ok": False, "error": {"message": str(exc), "code": code}}
    except (KeyError, IndexError, TypeError, AttributeError, ValueError):
        code = 3
        result = {"ok": False, "error": {"message": "Unexpected backend response or invalid URL.", "code": code}}
    except KeyboardInterrupt:
        code = 130
        result = {"ok": False, "error": {"message": "Interrupted.", "code": code}}
    if json_output:
        print(json.dumps(result, ensure_ascii=True))
    elif code:
        if "data" in result:
            print(json.dumps(result["data"], indent=2, ensure_ascii=True))
        else:
            print(result["error"]["message"], file=sys.stderr)
    elif isinstance(data, dict) and args.command == "logs":
        print(data["content"])
    else:
        print(json.dumps(data, indent=2, ensure_ascii=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
