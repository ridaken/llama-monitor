"""Optional inference-only listener and best-effort request capture."""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
import ipaddress
import socket
import time
from urllib.parse import urlparse
import uuid

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.requests import ClientDisconnect

import store
from proxy_capture import CAPTURE_PATHS, Capture, ResponseCapture
from proxy_history import RequestHistory

HOP_HEADERS = {b"connection", b"keep-alive", b"proxy-authenticate", b"proxy-authorization",
               b"te", b"trailer", b"transfer-encoding", b"upgrade"}


def forwarded_headers(headers, *, request=False):
    excluded = HOP_HEADERS.copy()
    for name, value in headers:
        if name.lower() == b"connection":
            excluded.update(item.strip().lower() for item in value.split(b","))
    if request:
        excluded.update((b"host", b"accept-encoding", b"x-llama-monitor-request-id"))
    else:
        excluded.add(b"x-llama-monitor-request-id")
    return [(name, value) for name, value in headers if name.lower() not in excluded]


class Listener(uvicorn.Server):
    def capture_signals(self):
        # The dashboard's outer server owns the process signal handlers.
        return nullcontext()

    def install_signal_handlers(self):  # uvicorn < 0.30
        pass


class Proxy:
    def __init__(self, db, target, *, host="127.0.0.1", port=8501, dashboard_port=8500):
        self.host, self.port, self.dashboard_port = host, port, dashboard_port
        self.target = target
        self.listener_error = None
        self.capture_error = None
        self.history = None
        try:
            self.history = RequestHistory(db)
        except Exception:
            self.capture_error = "Request history could not be initialized. Inference can continue without capture."
        self.server = None
        self.task = None
        self.client = None
        self.lock = asyncio.Lock()
        self.app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
        self.app.add_api_route("/{path:path}", self.forward, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"])

    async def start(self):
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5, pool=5),
                                        trust_env=False, follow_redirects=False)
        await self.configure({})

    async def close(self):
        await self.stop_listener()
        if self.client:
            await self.client.aclose()

    async def stop_listener(self):
        if self.server:
            self.server.should_exit = True
        if self.task:
            try:
                await asyncio.wait_for(asyncio.shield(self.task), 8)
            except asyncio.TimeoutError:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
        self.task = self.server = None

    async def configure(self, changes):
        allowed = {"enabled", "log_prompts", "log_responses"}
        if set(changes) - allowed or any(type(value) is not bool for value in changes.values()):
            raise ValueError("Proxy settings must contain only enabled, log_prompts, log_responses boolean values.")
        async with self.lock:
            if changes:
                await asyncio.to_thread(store.update_proxy, **changes)
            settings = await asyncio.to_thread(store.get_proxy)
            if not settings["enabled"]:
                await self.stop_listener()
                self.listener_error = None
            elif not self.task or self.task.done():
                self.listener_error = None
                sock = None
                try:
                    if self.port == self.dashboard_port:
                        raise ValueError("Proxy and dashboard ports must differ.")
                    family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
                    sock = socket.create_server((self.host, self.port), family=family)
                    sock.setblocking(False)
                    self.server = Listener(uvicorn.Config(self.app, host=self.host, port=self.port,
                        lifespan="off", log_config=None, access_log=False, log_level="warning", timeout_graceful_shutdown=5))
                    self.task = asyncio.create_task(self._serve(sock))
                    for _ in range(200):
                        if self.server.started or self.task.done():
                            break
                        await asyncio.sleep(0.01)
                    if not self.server.started:
                        raise RuntimeError("Listener did not start.")
                except (OSError, ValueError, RuntimeError):
                    if sock:
                        sock.close()
                    await self.stop_listener()
                    self.listener_error = "Proxy listener could not start. Check its bind address and port."
        return await self.state()

    async def _serve(self, sock):
        try:
            await self.server.serve(sockets=[sock])
        except (Exception, SystemExit):
            self.listener_error = "Proxy listener stopped unexpectedly. Disable and re-enable it to retry."
        finally:
            sock.close()

    async def state(self):
        settings = await asyncio.to_thread(store.get_proxy)
        target = await asyncio.to_thread(self.target)
        hostname = f"[{self.host}]" if ":" in self.host else self.host
        return {**settings, "host": self.host, "port": self.port,
                "base_url": f"http://{hostname}:{self.port}/v1",
                "target": target.get("url") if target else None,
                "listening": bool(self.server and self.server.started and self.task and not self.task.done()),
                "listener_error": self.listener_error, "capture_error": self.capture_error}

    async def save(self, operation, *args):
        if self.history:
            try:
                return await asyncio.to_thread(getattr(self.history, operation), *args)
            except Exception:
                self.capture_error = "Request capture failed. Some requests or content may be missing; inference continues."
        return None

    async def validate_target(self, target):
        try:
            parsed = urlparse(target)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
                return False
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError:
            return False
        if port not in (self.port, self.dashboard_port):
            return True
        def addresses(host):
            result = set()
            for item in socket.getaddrinfo(host, None):
                address = ipaddress.ip_address(item[4][0].split("%")[0])
                if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
                    address = address.ipv4_mapped
                result.add(str(address))
            return result
        try:
            local = {"127.0.0.1", "::1", "0.0.0.0", "::"}
            local.update(await asyncio.to_thread(addresses, socket.gethostname()))
            if self.host not in ("0.0.0.0", "::"):
                local.update(await asyncio.to_thread(addresses, self.host))
            remote = await asyncio.to_thread(addresses, parsed.hostname)
            return not (local & remote or any(ipaddress.ip_address(address).is_loopback for address in remote))
        except OSError:
            return False

    async def forward(self, request: Request):
        settings = await asyncio.to_thread(store.get_proxy)
        if not settings["enabled"]:
            return JSONResponse({"error": "Proxy is disabled."}, status_code=503)
        target = await asyncio.to_thread(self.target)
        if not target:
            return JSONResponse({"error": "Inference target is stopped or switching."}, status_code=503)
        if not await self.validate_target(target["url"]):
            return JSONResponse({"error": "Invalid upstream or proxy loop detected."}, status_code=502)
        started = time.monotonic()
        identifier = uuid.uuid4().hex
        capture = request.method == "POST" and request.url.path in CAPTURE_PATHS
        incoming = Capture(settings["log_prompts"])
        outgoing = ResponseCapture(settings["log_responses"], started)
        metadata = {"id": identifier, "observed_at": time.time(), "endpoint": request.url.path,
                    "method": request.method, "target": target["url"], "run_id": target.get("run_id"),
                    "state": "running", "status_code": None, "model": target.get("model"),
                    "prompt_logging": settings["log_prompts"], "response_logging": settings["log_responses"]}
        if capture:
            await self.save("begin", metadata)
        upload_complete = asyncio.Event()

        async def upload():
            async for chunk in request.stream():
                if capture:
                    incoming.feed(chunk)
                yield chunk
            upload_complete.set()

        async def disconnected():
            await upload_complete.wait()
            while True:
                if (await request.receive())["type"] == "http.disconnect":
                    return

        async def finish(state):
            if not capture:
                return
            try:
                prompt, model, truncated = incoming.request()
                response = outgoing.finish()
                metadata.update(model=outgoing.model or model or metadata["model"], state=state,
                    completed_at=time.time(), duration_seconds=time.monotonic() - started,
                    first_token_seconds=outgoing.first_token, usage=outgoing.usage,
                    request_truncated=truncated, response_truncated=outgoing.truncated,
                    capture_invalid=outgoing.invalid)
                if state == "complete" and outgoing.sse and not outgoing.done:
                    metadata["state"] = "incomplete"
                if outgoing.failed:
                    metadata["state"] = "error"
                await self.save("finish", metadata, prompt, response)
            except Exception:
                self.capture_error = "Request content could not be interpreted or saved. Inference continues."

        path = request.scope.get("raw_path", request.url.path.encode()).decode("ascii")
        url = target["url"].rstrip("/") + "/" + path.lstrip("/")
        if request.scope.get("query_string"):
            url += "?" + request.scope["query_string"].decode("ascii")
        headers = forwarded_headers(request.headers.raw, request=True)
        headers.append((b"accept-encoding", b"identity"))
        headers.append((b"x-llama-monitor-request-id", identifier.encode()))
        upstream = self.client.build_request(request.method, url, headers=headers, content=upload())
        send = asyncio.create_task(self.client.send(upstream, stream=True))
        watch = asyncio.create_task(disconnected())
        try:
            completed, _ = await asyncio.wait((send, watch), return_when=asyncio.FIRST_COMPLETED)
            if watch in completed:
                send.cancel()
                results = await asyncio.gather(send, return_exceptions=True)
                if results and isinstance(results[0], httpx.Response):
                    await results[0].aclose()
                await finish("cancelled")
                return JSONResponse({"error": "Client disconnected."}, status_code=499)
            response = await send
        except asyncio.CancelledError:
            send.cancel()
            await asyncio.gather(send, return_exceptions=True)
            await finish("cancelled")
            raise
        except ClientDisconnect:
            await finish("cancelled")
            return JSONResponse({"error": "Client disconnected during upload."}, status_code=499)
        except httpx.HTTPError as exc:
            metadata["status_code"] = 504 if isinstance(exc, httpx.TimeoutException) else 502
            await finish("error")
            return JSONResponse({"error": "Upstream connection failed."}, status_code=metadata["status_code"],
                                headers={"X-Llama-Monitor-Request-ID": identifier})
        finally:
            watch.cancel()
            await asyncio.gather(watch, return_exceptions=True)
        metadata["status_code"] = response.status_code
        outgoing.sse = "text/event-stream" in response.headers.get("content-type", "")

        async def stream():
            outcome = "cancelled"
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
                    if capture:
                        try:
                            outgoing.feed(chunk)
                        except Exception:
                            outgoing.invalid = True
                            self.capture_error = "Some response content could not be interpreted. Inference continues."
                outcome = "error" if response.status_code >= 400 else "complete"
            except httpx.HTTPError:
                outcome = "incomplete"
                raise
            finally:
                # Starlette cancels its streaming task on disconnect; cleanup
                # must survive that cancellation to close inference upstream.
                import anyio
                with anyio.CancelScope(shield=True):
                    await response.aclose()
                    await finish(outcome)

        result = StreamingResponse(stream(), status_code=response.status_code)
        result.raw_headers = forwarded_headers(response.headers.raw) + [(b"x-llama-monitor-request-id", identifier.encode())]
        return result
