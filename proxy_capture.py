"""Bounded, best-effort interpretation of completion JSON and SSE streams."""
from __future__ import annotations

import codecs
import json
import time

MAX_PAYLOAD = 1024 * 1024
CAPTURE_PATHS = {"/v1/chat/completions", "/v1/completions", "/completion"}


def omit_media(value):
    if isinstance(value, dict):
        if value.get("type") in ("image_url", "input_image", "input_audio", "audio", "video", "video_url"):
            return {"type": value["type"], "omitted": "Binary/image content is not captured"}
        return {key: ("[media omitted]" if key in ("image_data", "audio_data") else omit_media(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [omit_media(item) for item in value]
    if isinstance(value, str) and value.startswith("data:"):
        return "[media omitted]"
    return value


def formatted(value):
    text = json.dumps(omit_media(value), ensure_ascii=False, indent=2)
    raw = text.encode("utf-8")
    return raw[:MAX_PAYLOAD].decode("utf-8", "ignore"), len(raw) > MAX_PAYLOAD


class Capture:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.raw = bytearray()
        self.truncated = False

    def feed(self, chunk):
        remaining = MAX_PAYLOAD - len(self.raw)
        self.raw.extend(chunk[:remaining])
        self.truncated |= len(chunk) > remaining

    def request(self):
        try:
            body = json.loads(self.raw)
            text, truncated = formatted(body)
            return text if self.enabled else None, body.get("model") if isinstance(body, dict) else None, truncated or self.truncated
        except (ValueError, UnicodeError, RecursionError):
            return ("[Request is too large or is not readable JSON]" if self.enabled else None), None, self.truncated


class ResponseCapture(Capture):
    def __init__(self, enabled, started):
        super().__init__(enabled)
        self.started = started
        self.first_token = None
        self.usage = None
        self.model = None
        self.done = False
        self.failed = False
        self.sse = False
        self.invalid = False
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.line = ""
        self.data = []
        self.choices = {}
        self.saved_bytes = 0

    def observe(self, body):
        if not isinstance(body, dict):
            return
        if body.get("error"):
            self.failed = True
        if isinstance(body.get("model"), str):
            self.model = body["model"]
        if isinstance(body.get("usage"), dict):
            self.usage = body["usage"]
        if isinstance(body.get("timings"), dict):
            timing = body["timings"]
            self.usage = {"prompt_tokens": timing.get("prompt_n"), "completion_tokens": timing.get("predicted_n")}
        content = body.get("content")
        choices = body.get("choices") or []
        generated = bool(content) or any(
            isinstance(c, dict) and (c.get("text") or any((c.get("delta") or c.get("message") or {}).get(key)
            for key in ("content", "reasoning_content", "reasoning", "tool_calls"))) for c in choices)
        if generated and self.first_token is None:
            self.first_token = time.monotonic() - self.started
        self.done |= body.get("stop") is True
        if not self.enabled:
            return
        encoded_size = len(json.dumps(body).encode())
        if self.saved_bytes + encoded_size > MAX_PAYLOAD:
            self.truncated = True
            return
        self.saved_bytes += encoded_size
        if isinstance(content, str):
            target = self.choices.setdefault(0, {"index": 0, "content": "", "tool_calls": {}})
            target["content"] += content
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            target = self.choices.setdefault(choice.get("index", 0), {"index": choice.get("index", 0), "tool_calls": {}})
            delta = choice.get("delta") or choice.get("message") or {}
            for key in ("content", "reasoning_content", "reasoning"):
                value = delta.get(key)
                if isinstance(value, str):
                    target[key] = target.get(key, "") + value
            if isinstance(choice.get("text"), str):
                target["text"] = target.get("text", "") + choice["text"]
            if choice.get("finish_reason") is not None:
                target["finish_reason"] = choice["finish_reason"]
            for position, tool in enumerate(delta.get("tool_calls") or []):
                slot = target["tool_calls"].setdefault(tool.get("index", position), {"function": {}})
                for key in ("id", "type"):
                    if key in tool:
                        slot[key] = tool[key]
                for key, value in (tool.get("function") or {}).items():
                    if isinstance(value, str):
                        slot["function"][key] = slot["function"].get(key, "") + value

    def feed(self, chunk):
        if not self.sse:
            super().feed(chunk)
            return
        # Bound an individual line/event even when response logging is disabled.
        text = self.decoder.decode(chunk)
        for part in text.splitlines(keepends=True):
            self.line += part
            if len(self.line) > MAX_PAYLOAD:
                self.line = ""
                self.data = []
                self.invalid = True
                self.truncated = True
            if part.endswith("\n"):
                line, self.line = self.line.rstrip("\r\n"), ""
                if not line:
                    data, self.data = "\n".join(self.data), []
                    if data == "[DONE]":
                        self.done = True
                    elif data:
                        try:
                            self.observe(json.loads(data))
                        except (ValueError, TypeError, AttributeError, RecursionError):
                            self.invalid = True
                elif line.startswith("data:"):
                    self.data.append(line[5:].lstrip(" "))
                    if sum(map(len, self.data)) > MAX_PAYLOAD:
                        self.data = []
                        self.invalid = self.truncated = True

    def finish(self):
        if not self.sse:
            try:
                body = json.loads(self.raw)
                self.observe(body)
                self.done = True
                text, clipped = formatted(body)
                self.truncated |= clipped
                return text if self.enabled else None
            except (ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
                self.invalid = True
                return "[Response is too large or is not readable JSON; content unavailable]" if self.enabled else None
        if not self.enabled:
            return None
        choices = []
        for value in self.choices.values():
            choices.append({**value, "tool_calls": list(value["tool_calls"].values())})
        text, clipped = formatted({"choices": choices, "usage": self.usage})
        self.truncated |= clipped
        return text
