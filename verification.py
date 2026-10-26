"""Explicit inference probes, independent of cached telemetry."""
from __future__ import annotations

import asyncio
import time

import httpx
import psutil
from pydantic import BaseModel, Field

import store
from processes import identity_matches, same_identity
from launcher import LaunchError


class SmokeRequest(BaseModel):
    timeout: float = Field(default=60, gt=0, le=300, allow_inf_nan=False)


TEST_PROMPT = ("Write a concise Python 3 program that prints FizzBuzz for integers "
               "1 through 100. Output only the code.")


class TestPromptRequest(BaseModel):
    timeout: float = Field(default=120, gt=0, le=300, allow_inf_nan=False)


class VerificationError(RuntimeError):
    pass


class VerificationTimeout(VerificationError):
    pass


def owns_port(snapshot):
    return identity_matches(snapshot) and any(
        connection.status == psutil.CONN_LISTEN and connection.laddr.port == snapshot["port"]
        for connection in psutil.Process(snapshot["pid"]).net_connections(kind="tcp"))


class ManagedVerification:
    def __init__(self, manager, gaming):
        self.manager = manager
        self.gaming = gaming

    def _snapshot(self):
        try:
            snapshot = self.manager.snapshot()
        except (LaunchError, RuntimeError, psutil.Error) as exc:
            raise VerificationError("Cannot verify managed process identity.") from exc
        record = store.get_running() or {}
        if snapshot is None or record.get("pid") != snapshot["pid"] or not record.get("run_id"):
            raise VerificationError("Managed process identity is unavailable.")
        snapshot["run_id"] = record["run_id"]
        return snapshot

    def _unchanged(self, snapshot):
        current = self._snapshot()
        if current["run_id"] != snapshot["run_id"] or not same_identity(snapshot, current):
            raise VerificationError("Managed server changed during verification.")
        if self.gaming.state()["blocked"]:
            raise VerificationError("Model switching or recovery blocks verification.")
        try:
            owns_listener = owns_port(current)
        except (RuntimeError, psutil.Error) as exc:
            raise VerificationError("Cannot verify ownership of the inference listener.") from exc
        if not owns_listener:
            raise VerificationError("Managed process does not own its inference port.")

    def readiness(self):
        try:
            return self._readiness()
        except (LaunchError, RuntimeError, psutil.Error) as exc:
            return {"state": "failed", "ready": False, "run_id": None, "pid": None,
                    "reason": "Cannot verify managed process state."}

    def _readiness(self):
        status = self.manager.status()
        result = {"state": "stopped", "ready": False, "run_id": None,
                  "pid": status.get("pid"), "reason": "No managed server is running."}
        if self.gaming.state()["blocked"]:
            return {**result, "state": "blocked", "reason": "Model switching or recovery is pending."}
        if status["state"] == "exited":
            return {**result, "state": "failed", "reason": f"Server exited with code {status.get('exit_code')}."}
        if status["state"] != "running":
            return result
        try:
            snapshot = self._snapshot()
            result.update(state="loading", run_id=snapshot["run_id"], reason="Waiting for the managed inference listener.")
            if not owns_port(snapshot):
                return result
            with httpx.Client(timeout=1, trust_env=False, follow_redirects=False) as client:
                response = client.get(f"http://127.0.0.1:{snapshot['port']}/health")
                healthy = response.status_code == 200 and response.json().get("status") == "ok"
            self._unchanged(snapshot)
            if healthy:
                return {**result, "state": "ready", "ready": True, "reason": "Managed model is ready."}
            return {**result, "reason": "Managed model is still loading or unhealthy."}
        except httpx.HTTPError:
            return {**result, "reason": "Managed model health endpoint is not available yet."}
        except (RuntimeError, LaunchError, psutil.Error, ValueError, AttributeError) as exc:
            return {**result, "state": "failed", "reason": str(exc)}

    async def smoke(self, timeout):
        started = time.monotonic()
        try:
            ready = await asyncio.wait_for(asyncio.to_thread(self.readiness), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise VerificationTimeout("Readiness verification timed out.") from exc
        if not ready["ready"]:
            raise VerificationError(ready["reason"])
        snapshot = self._snapshot()
        if snapshot["run_id"] != ready["run_id"]:
            raise VerificationError("Managed server changed before inference.")
        self._unchanged(snapshot)
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise VerificationTimeout("Inference verification timed out.")

        async def complete():
            async with httpx.AsyncClient(timeout=remaining, trust_env=False, follow_redirects=False) as client:
                response = await client.post(f"http://127.0.0.1:{snapshot['port']}/v1/chat/completions", json={
                    "messages": [{"role": "user", "content": "Reply with OK."}],
                    "temperature": 0, "max_tokens": 16, "stream": False})
                response.raise_for_status()
                return response.json()

        try:
            body = await asyncio.wait_for(complete(), timeout=remaining)
        except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
            raise VerificationTimeout("Inference verification timed out; the server may still finish the request.") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise VerificationError("Managed inference request failed or returned invalid JSON.") from exc
        self._unchanged(snapshot)
        if time.monotonic() - started >= timeout:
            raise VerificationTimeout("Inference verification deadline exceeded.")
        try:
            choice = body["choices"][0]
            message = choice["message"]
            content = message.get("content") or message.get("reasoning_content") or message.get("reasoning")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty completion")
        except (KeyError, IndexError, TypeError, AttributeError, ValueError) as exc:
            raise VerificationError("Inference returned no generated content.") from exc
        return {"run_id": snapshot["run_id"], "elapsed_seconds": time.monotonic() - started,
                "message": message, "finish_reason": choice.get("finish_reason"), "usage": body.get("usage")}


class InferenceTest:
    """One explicit request through the current configured inference route."""

    def __init__(self, verification, proxy):
        self.verification = verification
        self.proxy = proxy
        self.lock = asyncio.Lock()

    async def run(self, timeout):
        if self.lock.locked():
            raise VerificationError("A test prompt is already running. Wait for it to finish.")
        async with self.lock:
            try:
                return await asyncio.wait_for(self._run(timeout), timeout=timeout)
            except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
                raise VerificationTimeout("Test prompt timed out; the model may still finish the request.") from exc
            except httpx.HTTPStatusError as exc:
                raise VerificationError(f"Test prompt returned HTTP {exc.response.status_code}. Check the inference server or proxy.") from exc
            except httpx.HTTPError as exc:
                raise VerificationError("Cannot connect to the configured inference route or read its response.") from exc

    async def _run(self, timeout):
        started = time.monotonic()
        target = await asyncio.to_thread(self.proxy.target)
        if not target:
            raise VerificationError("Inference target is stopped or switching.")
        if not await self.proxy.validate_target(target["url"]):
            raise VerificationError("Invalid inference target or proxy loop detected.")
        state = await self.proxy.state()
        route = "proxy" if state["enabled"] else "direct"
        if route == "proxy" and not state["listening"]:
            raise VerificationError(state.get("listener_error") or "The enabled proxy is not listening.")

        snapshot = None
        if target["managed"]:
            ready = await asyncio.to_thread(self.verification.readiness)
            if not ready["ready"]:
                raise VerificationError(ready["reason"])
            snapshot = await asyncio.to_thread(self.verification._snapshot)
            if ready["run_id"] != target["run_id"] or snapshot["run_id"] != target["run_id"]:
                raise VerificationError("Managed server changed before the test prompt.")

        async def unchanged():
            if await asyncio.to_thread(self.proxy.target) != target:
                raise VerificationError("Inference target changed during the test prompt.")
            if snapshot:
                await asyncio.to_thread(self.verification._unchanged, snapshot)

        async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False) as client:
            if not target["managed"]:
                health = await client.get(target["url"].rstrip("/") + "/health", timeout=min(timeout, 5))
                if health.status_code != 200:
                    raise VerificationError("Inference target is loading or unhealthy.")
                try:
                    if health.json().get("status") != "ok":
                        raise ValueError("unhealthy")
                except (ValueError, AttributeError) as exc:
                    raise VerificationError("Inference target did not return a healthy status.") from exc
            await unchanged()
            # Wildcard listener addresses are bind addresses, not destinations.
            host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(state["host"], state["host"])
            host = f"[{host}]" if ":" in host else host
            base = f"http://{host}:{state['port']}" if route == "proxy" else target["url"].rstrip("/")
            response = await client.post(base + "/v1/chat/completions", json={
                "messages": [{"role": "user", "content": TEST_PROMPT}],
                "temperature": 0, "max_tokens": 2048, "stream": False})
            response.raise_for_status()
            await unchanged()
            try:
                body = response.json()
                choice = body["choices"][0]
                message = choice["message"]
                texts = [message.get(key) for key in ("content", "reasoning_content", "reasoning")]
                if not any(isinstance(text, str) and text.strip() for text in texts):
                    raise ValueError("empty completion")
                if any(text is not None and not isinstance(text, str) for text in texts):
                    raise ValueError("invalid completion text")
            except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
                raise VerificationError("Test prompt returned invalid JSON or no generated text.") from exc
        return {"route": route, "target": target["url"], "model": body.get("model") or target.get("model"),
                "run_id": target.get("run_id"), "prompt": TEST_PROMPT, "message": message,
                "elapsed_seconds": time.monotonic() - started, "finish_reason": choice.get("finish_reason"),
                "usage": body.get("usage"),
                "request_id": response.headers.get("x-llama-monitor-request-id") if route == "proxy" else None}
