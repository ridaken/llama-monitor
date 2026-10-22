"""Explicit probes of the managed model, independent of cached telemetry."""
from __future__ import annotations

import asyncio
import time

import httpx
import psutil
from pydantic import BaseModel, Field

import store
from processes import identity_matches
from launcher import LaunchError


class SmokeRequest(BaseModel):
    timeout: float = Field(default=60, gt=0, le=300, allow_inf_nan=False)


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
        if any(current[key] != snapshot[key] for key in ("pid", "created_at", "run_id")):
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
