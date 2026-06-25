"""Data collectors for the llama-server live monitor.

Three families of data are gathered, all on the local machine:
  * llama-server HTTP endpoints  -> model info + throughput metrics
  * NVML (via pynvml)            -> per-GPU hardware stats + per-process VRAM
  * llama-server startup log     -> CPU/system-RAM portion of the model split

Everything degrades gracefully: if llama-server is down we still return GPU
stats, and if NVML fails we still return the llama metrics.
"""

from __future__ import annotations

import copy
import os
import re
import time
from typing import Optional

import httpx

try:
    import pynvml  # provided by the `nvidia-ml-py` package
except Exception:  # pragma: no cover - import guarded so the app still boots
    pynvml = None

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None


# --------------------------------------------------------------------------- #
# Prometheus text parsing                                                      #
# --------------------------------------------------------------------------- #

# Matches simple `metric_name value` lines (ignores HELP/TYPE comments and any
# label sets — llama.cpp's metrics are unlabeled so the simple form is enough).
_METRIC_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)\s+([-+0-9.eE]+)\s*$")


def parse_prometheus(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = _METRIC_RE.match(line.strip())
        if m:
            try:
                out[m.group(1)] = float(m.group(2))
            except ValueError:
                pass
    return out


# --------------------------------------------------------------------------- #
# llama-server HTTP collector                                                  #
# --------------------------------------------------------------------------- #

class LlamaCollector:
    """Polls llama-server for model info, throughput and slot state.

    Live decode tok/s is derived from per-slot ``n_decoded`` deltas (which tick
    continuously during generation), because llama's ``*_total`` metric counters
    only update when a request *finishes* and so can't drive a live readout.
    """

    def __init__(self, base_url: str, timeout: float = 2.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout)
        self._props: dict = {}
        self._props_ts: float = 0.0
        # Per-slot generated-token counts from the previous poll, for live rate.
        self._prev_decoded: Optional[dict[int, int]] = None
        self._prev_decoded_t: float = 0.0
        self._last_produced: int = 0  # tokens generated during the last interval
        # Speculative decoding: latched once observed, plus n_decode counter.
        self._spec_enabled: bool = False
        self._ndecode_total: Optional[float] = None
        self._prev_ndecode: Optional[float] = None
        # Prompt-prefill timing, from llama's own cumulative counters.
        self._prev_prompt_tok: Optional[float] = None
        self._prev_prompt_sec: float = 0.0
        self._last_prefill: Optional[dict] = None
        # Cached metrics-derived fields, reused in lite (idle) polls that skip
        # /metrics to avoid waking the server loop while nothing is generating.
        self._last_tp: Optional[dict] = None
        self._last_requests: Optional[dict] = None
        self._last_metrics_enabled: bool = False
        # Whole last result, returned frozen on "none" polls (log-driven idle,
        # which contacts the server not at all).
        self._last_result: Optional[dict] = None

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str) -> Optional[httpx.Response]:
        try:
            r = self._client.get(self.base_url + path)
            if r.status_code == 200:
                return r
        except Exception:
            return None
        return None

    def _props_cached(self) -> dict:
        # /props rarely changes; refresh at most every 15s.
        now = time.time()
        if now - self._props_ts > 15 or not self._props:
            r = self._get("/props")
            if r is not None:
                try:
                    self._props = r.json()
                    self._props_ts = now
                except Exception:
                    pass
        return self._props

    def collect(self, level: str = "full") -> dict:
        """Poll llama-server at one of three levels:

        * ``"full"``  — /props (cached) + /metrics + /slots (active generation).
        * ``"slots"`` — /props (cached) + /slots only; /metrics reused from cache
          (idle fallback when there is no log to watch).
        * ``"none"``  — no HTTP at all: returns the last result frozen. Used for
          log-driven idle so the server's loop is never woken.
        """
        if level == "none":
            if self._last_result is not None:
                frozen = copy.deepcopy(self._last_result)
                if frozen.get("throughput"):
                    frozen["throughput"]["decode_tps_live"] = None
                frozen["stale"] = True
                return frozen
            level = "full"  # nothing cached yet -> bootstrap with a real poll

        result: dict = {"online": False}

        props = self._props_cached()
        if props:
            result["online"] = True
            gen = props.get("default_generation_settings", {}) or {}
            model_path = props.get("model_path") or gen.get("model") or ""
            result["model"] = {
                "name": props.get("model_alias")
                or os.path.basename(model_path)
                or "unknown",
                "path": model_path or None,
                "n_ctx": gen.get("n_ctx") or props.get("n_ctx"),
                "total_slots": props.get("total_slots"),
            }

        # Fallback model id from the OpenAI-compatible endpoint.
        if not result.get("model"):
            r = self._get("/v1/models")
            if r is not None:
                try:
                    data = r.json().get("data", [])
                    if data:
                        result["online"] = True
                        result["model"] = {"name": data[0].get("id", "unknown")}
                except Exception:
                    pass

        # /metrics (requires llama-server --metrics). Skipped at "slots" level;
        # the last-known throughput/requests are reused instead (they're "last
        # request" values that don't change while idle anyway).
        if level == "slots":
            if self._last_tp is not None:
                result["throughput"] = dict(self._last_tp)
                result["throughput"]["decode_tps_live"] = None
            if self._last_requests is not None:
                result["requests"] = dict(self._last_requests)
            result["metrics_enabled"] = self._last_metrics_enabled
            if self._last_tp is not None:
                result["online"] = True
        else:
            metrics_present = False
            r = self._get("/metrics")
            if r is not None:
                m = parse_prometheus(r.text)
                if m:
                    metrics_present = True
                    result["online"] = True
                    self._apply_metrics(result, m)
            result["metrics_enabled"] = metrics_present

        # /slots (enabled by default) — source of live decode tok/s, per-slot
        # detail, KV-cache usage (no kv metric in this build) and spec detection.
        live_decode = None
        r = self._get("/slots")
        if r is not None:
            try:
                slots = r.json()
                if isinstance(slots, list):
                    busy = sum(1 for s in slots if s.get("is_processing"))
                    n_ctx = (result.get("model") or {}).get("n_ctx")
                    result["slots"] = {
                        "total": len(slots),
                        "busy": busy,
                        "list": [self._slot_detail(s, n_ctx) for s in slots],
                    }
                    live_decode = self._live_decode_tps(slots)
                    self._apply_kv_from_slots(result, slots, n_ctx)
                    self._detect_spec(slots)
            except Exception:
                pass

        if live_decode is not None:
            result.setdefault("throughput", {})["decode_tps_live"] = live_decode

        # Speculative-decode efficiency = accepted tokens per decode step, per
        # sequence. llama batches concurrent slots into one llama_decode() call,
        # so we divide by the number of busy slots to get the per-sequence figure
        # (≈1 means speculation isn't helping; up to spec_draft_n_max+1 is ideal).
        spec_eff = None
        ndecode = self._ndecode_total
        busy = (result.get("slots") or {}).get("busy") or 0
        if ndecode is not None and self._prev_ndecode is not None:
            d_calls = ndecode - self._prev_ndecode
            if d_calls > 0 and self._last_produced > 0 and busy > 0:
                spec_eff = (self._last_produced / d_calls) / busy
        self._prev_ndecode = ndecode
        result["spec"] = {"enabled": self._spec_enabled, "tokens_per_decode": spec_eff}
        result["prefill_last"] = self._last_prefill

        self._last_result = result
        return result

    def _slot_detail(self, sl: dict, n_ctx) -> dict:
        proc = bool(sl.get("is_processing"))
        decoded = self._slot_decoded(sl) or 0
        p_tok = int(sl.get("n_prompt_tokens") or 0)
        p_done = int(sl.get("n_prompt_tokens_processed") or 0)
        if not proc:
            state = "idle"
        elif decoded > 0:
            state = "generating"
        else:
            state = "prefill"
        used = p_tok + decoded
        return {
            "id": sl.get("id"),
            "state": state,
            "prompt_tokens": p_tok,
            "prompt_processed": p_done,
            "prefill_ratio": (p_done / p_tok) if (state == "prefill" and p_tok) else None,
            "decoded": decoded,
            "ctx_used": used,
            "ctx_ratio": (used / n_ctx) if n_ctx else None,
        }

    def _detect_spec(self, slots: list) -> None:
        """Latch speculative-decoding-enabled once observed from any slot."""
        if self._spec_enabled:
            return
        for sl in slots:
            if sl.get("speculative"):
                self._spec_enabled = True
                return
            types = (sl.get("params") or {}).get("speculative.types")
            if isinstance(types, str) and any(
                t.strip() not in ("", "none") for t in types.split(",")
            ):
                self._spec_enabled = True
                return

    def _apply_kv_from_slots(self, result: dict, slots: list, n_ctx) -> None:
        """Estimate KV-cache occupancy = tokens held across all slots / n_ctx.

        With kv_unified=true the cache of size n_ctx is shared across slots, so
        the sum of each slot's (prompt + decoded) tokens is total occupancy.
        """
        # Prefer a real metric if the server exposes one.
        if (result.get("kv") or {}).get("usage_ratio") is not None:
            return
        used = 0
        for sl in slots:
            used += int(sl.get("n_prompt_tokens") or 0)
            nd = self._slot_decoded(sl)
            used += nd or 0
        ratio = (used / n_ctx) if n_ctx else None
        if ratio is not None:
            ratio = max(0.0, min(1.0, ratio))
        result["kv"] = {"usage_ratio": ratio, "tokens": used}

    @staticmethod
    def _slot_decoded(sl: dict) -> Optional[int]:
        """Extract the running generated-token count for a slot, if present."""
        nt = sl.get("next_token")
        if isinstance(nt, list) and nt and isinstance(nt[0], dict):
            if nt[0].get("n_decoded") is not None:
                return int(nt[0]["n_decoded"])
        if sl.get("n_decoded") is not None:  # older/flat schema
            return int(sl["n_decoded"])
        return None

    def _live_decode_tps(self, slots: list) -> Optional[float]:
        """Live decode tok/s from the change in per-slot n_decoded since last poll."""
        now = time.time()
        cur: dict[int, int] = {}
        for sl in slots:
            if not sl.get("is_processing"):
                continue
            nd = self._slot_decoded(sl)
            if nd is not None:
                cur[sl.get("id")] = nd

        rate = None
        produced = 0
        # Only report a live rate while something is actually generating; when
        # idle we return None so the UI falls back to the last-request average.
        if cur and self._prev_decoded is not None:
            dt = now - self._prev_decoded_t
            if dt > 0:
                for sid, nd in cur.items():
                    prev = self._prev_decoded.get(sid)
                    if prev is None:
                        continue  # slot started generating this interval; wait
                    produced += nd - prev if nd >= prev else nd  # restart -> from 0
                rate = produced / dt
        self._prev_decoded = cur
        self._prev_decoded_t = now
        self._last_produced = produced  # for spec-decode efficiency
        return rate

    def _apply_metrics(self, result: dict, m: dict[str, float]) -> None:
        result["throughput"] = {
            # Live decode rate is filled in later from /slots (continuous).
            "decode_tps_live": None,
            # llama's own per-request average gauges (update on completion).
            "decode_tps_avg": m.get("llamacpp:predicted_tokens_seconds"),
            "pp_tps_avg": m.get("llamacpp:prompt_tokens_seconds"),
        }
        result["kv"] = {
            "usage_ratio": m.get("llamacpp:kv_cache_usage_ratio"),
            "tokens": m.get("llamacpp:kv_cache_tokens"),
        }
        result["requests"] = {
            "processing": m.get("llamacpp:requests_processing"),
            "deferred": m.get("llamacpp:requests_deferred"),
        }
        self._last_tp = result["throughput"]
        self._last_requests = result["requests"]
        self._last_metrics_enabled = True
        self._ndecode_total = m.get("llamacpp:n_decode_total")

        # Exact last-prefill timing from llama's cumulative prompt counters:
        # the deltas at a completion give that request's prefilled tokens & time.
        p_tok = m.get("llamacpp:prompt_tokens_total")
        p_sec = m.get("llamacpp:prompt_seconds_total")
        if p_tok is not None and self._prev_prompt_tok is not None:
            d_tok = p_tok - self._prev_prompt_tok
            d_sec = (p_sec or 0.0) - self._prev_prompt_sec
            if d_tok > 0 and d_sec > 0:
                self._last_prefill = {
                    "tokens": int(d_tok),
                    "secs": d_sec,
                    "tps": d_tok / d_sec,
                }
        self._prev_prompt_tok = p_tok
        self._prev_prompt_sec = p_sec or 0.0


# --------------------------------------------------------------------------- #
# NVML / GPU collector                                                         #
# --------------------------------------------------------------------------- #

class GpuCollector:
    def __init__(self):
        self.ok = False
        self.error: Optional[str] = None
        if pynvml is None:
            self.error = "nvidia-ml-py (pynvml) not installed"
            return
        try:
            pynvml.nvmlInit()
            self.ok = True
        except Exception as e:  # pragma: no cover
            self.error = f"NVML init failed: {e}"

    def collect(self, llama_pids: set[int]) -> dict:
        if not self.ok:
            return {"ok": False, "error": self.error, "devices": []}

        devices = []
        try:
            count = pynvml.nvmlDeviceGetCount()
        except Exception as e:
            return {"ok": False, "error": str(e), "devices": []}

        for i in range(count):
            d: dict = {"index": i}
            try:
                h = pynvml.nvmlDeviceGetHandleByIndex(i)
                d["name"] = _decode(pynvml.nvmlDeviceGetName(h))
                mem = pynvml.nvmlDeviceGetMemoryInfo(h)
                d["mem_total"] = mem.total
                d["mem_used"] = mem.used
                try:
                    util = pynvml.nvmlDeviceGetUtilizationRates(h)
                    d["util_gpu"] = util.gpu
                    d["util_mem"] = util.memory
                except Exception:
                    pass
                try:
                    d["temp"] = pynvml.nvmlDeviceGetTemperature(
                        h, pynvml.NVML_TEMPERATURE_GPU
                    )
                except Exception:
                    pass
                try:
                    d["power"] = pynvml.nvmlDeviceGetPowerUsage(h) / 1000.0
                    d["power_limit"] = (
                        pynvml.nvmlDeviceGetEnforcedPowerLimit(h) / 1000.0
                    )
                except Exception:
                    pass
                # Per-process VRAM used by llama-server on this device.
                d["llama_mem"] = self._llama_mem_on(h, llama_pids)
            except Exception as e:
                d["error"] = str(e)
            devices.append(d)

        return {"ok": True, "devices": devices}

    @staticmethod
    def _llama_mem_on(handle, llama_pids: set[int]) -> Optional[int]:
        if not llama_pids:
            return None
        total = 0
        found = False
        for fn in (
            "nvmlDeviceGetComputeRunningProcesses_v3",
            "nvmlDeviceGetComputeRunningProcesses_v2",
            "nvmlDeviceGetComputeRunningProcesses",
        ):
            getter = getattr(pynvml, fn, None)
            if getter is None:
                continue
            try:
                for p in getter(handle):
                    if p.pid in llama_pids and p.usedGpuMemory not in (None, 0):
                        total += int(p.usedGpuMemory)
                        found = True
                break
            except Exception:
                continue
        return total if found else None

    def shutdown(self) -> None:
        if self.ok:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass


def _decode(v) -> str:
    return v.decode() if isinstance(v, (bytes, bytearray)) else str(v)


# --------------------------------------------------------------------------- #
# Process discovery + log parsing for the model split                          #
# --------------------------------------------------------------------------- #

def collect_sysmem(llama_pids: Optional[set[int]] = None) -> dict:
    """System RAM utilisation, plus llama-server's resident set size.

    Complements the GPU panel: when a model spills out of VRAM into system RAM,
    ``llama_rss`` (summed RSS of the llama-server process[es]) is where it shows
    up. Degrades to ``{"ok": False}`` if psutil isn't available.
    """
    if psutil is None:
        return {"ok": False, "error": "psutil not installed"}
    try:
        v = psutil.virtual_memory()
    except Exception as e:  # pragma: no cover - psutil failure is rare
        return {"ok": False, "error": str(e)}

    llama_rss: Optional[int] = None
    if llama_pids:
        total = 0
        found = False
        for pid in llama_pids:
            try:
                total += psutil.Process(pid).memory_info().rss
                found = True
            except Exception:
                continue
        llama_rss = total if found else None

    return {
        "ok": True,
        "total": v.total,
        "used": v.used,
        "available": v.available,
        "percent": v.percent,
        "llama_rss": llama_rss,
    }


def find_llama_pids(name_hint: str = "llama", port: Optional[int] = None) -> set[int]:
    """Find llama-server PID(s) by process name and/or the port it listens on."""
    pids: set[int] = set()
    if psutil is None:
        return pids
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            nm = (proc.info.get("name") or "").lower()
            if name_hint in nm:
                pids.add(proc.info["pid"])
        except Exception:
            continue
    if port:
        try:
            for c in psutil.net_connections(kind="inet"):
                if c.laddr and c.laddr.port == port and c.pid:
                    pids.add(c.pid)
        except Exception:
            pass
    return pids


# Per-device buffer allocations printed during model load. Matches e.g.:
#   "load_tensors:        CUDA0 model buffer size =  8500.00 MiB"
#   "llama_kv_cache: CUDA1 KV buffer size =  78.00 MiB"
#   "llama_context:      CUDA2 compute buffer size = 304.00 MiB"
#   "load_tensors:   CPU_Mapped model buffer size = 8123.45 MiB"
#   "ggml_cuda_host_malloc: ... CUDA_Host buffer size = 12.00 MiB"  (host pinned)
_BUF_RE = re.compile(
    r"\b(CUDA\d+|CUDA_Host|ROCm\d+|Vulkan\d+|SYCL\d+|Metal|CPU(?:_Mapped)?)\b"
    r"[^\n]*?buffer size\s*=\s*([0-9.]+)\s*MiB",
    re.IGNORECASE,
)

# llama prints a "build: <n> (<sha>) with ..." banner once per process start.
# We anchor on the last one so a restart appended to the same log file does not
# double-count earlier runs.
_BUILD_RE = re.compile(r"(?m)^build:\s", re.IGNORECASE)


def _norm_device(label: str) -> str:
    up = label.upper()
    # Host-side allocations (pinned host memory, CPU model buffer) -> system RAM.
    if up.startswith("CPU") or up == "CUDA_HOST":
        return "CPU"
    return up


def parse_device_split(log_path: str) -> dict[str, float]:
    """Sum per-device buffer allocations (MiB) from a llama-server startup log.

    Returns an ordered dict like {"CUDA0": 8804.0, "CUDA1": 7100.0, "CPU": 8123.45}
    covering model + KV + compute buffers for the most recent run. This is the
    reliable way to get the memory split on Windows/WDDM, where NVML cannot
    report per-process VRAM.
    """
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except Exception:
        return {}

    # Restrict to the latest server run if a build banner is present.
    starts = [m.start() for m in _BUILD_RE.finditer(text)]
    if starts:
        text = text[starts[-1]:]

    sums: dict[str, float] = {}
    for label, mib in _BUF_RE.findall(text):
        sums[_norm_device(label)] = sums.get(_norm_device(label), 0.0) + float(mib)

    # Order: CUDA0, CUDA1, … then any other accel, then CPU last.
    def sort_key(k: str):
        m = re.match(r"([A-Z]+)(\d+)", k)
        if k == "CPU":
            return (2, 0, "")
        if m:
            return (0, int(m.group(2)), m.group(1))
        return (1, 0, k)

    return {k: sums[k] for k in sorted(sums, key=sort_key)}


# llama-server logs a "device_info:" block at startup listing free memory per
# device, e.g.  "- CUDA0   : NVIDIA ... (12281 MiB, 11069 MiB free)".
_DEVINFO_RE = re.compile(
    r"-\s*(CUDA\d+|ROCm\d+|Vulkan\d+|SYCL\d+|CPU)\b[^\n(]*\(\s*\d+\s*MiB,\s*(\d+)\s*MiB free\)",
    re.IGNORECASE,
)


def parse_device_baseline(log_path: str) -> dict[str, float]:
    """Parse free-memory-per-device (MiB) from the latest 'device_info:' block.

    Used to estimate the model's per-device footprint as (free at load) minus
    (free now from NVML) — the only live split signal available on builds that
    don't print per-device buffer-size lines.
    """
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except Exception:
        return {}

    idx = text.lower().rfind("device_info:")
    if idx == -1:
        return {}
    segment = text[idx: idx + 4000]  # the device list is a short block

    out: dict[str, float] = {}
    for label, free in _DEVINFO_RE.findall(segment):
        out[label.upper()] = float(free)
    return out


_SPEC_RE = re.compile(r"draft-?mtp|speculative impl|\[spec\]", re.IGNORECASE)


def parse_spec_enabled(log_path: str) -> bool:
    """Detect speculative/MTP decoding from startup-log markers (idle-time signal).

    /props reports speculative.types=none by default, so the log is the only way
    to know MTP is configured before the first request arrives.
    """
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            return bool(_SPEC_RE.search(f.read()))
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Log tailer — activity detection + exact per-request stats from the log file  #
# --------------------------------------------------------------------------- #

class LogTailer:
    """Tails a llama-server log file to detect request activity and parse exact
    per-request stats, using only local file I/O — it never contacts the server,
    so it can't wake the idle loop.

    Robustness is deliberate: matching keys off short, stable substrings, and
    activity also falls back to a format-independent "did the file grow with
    non-idle content?" signal. If the format changes so much that nothing
    matches, the worst case is that ``poll()`` keeps reporting activity (the app
    then simply falls back to HTTP polling) — it never silently goes blind.
    """

    _ANSI = re.compile(r"\x1b\[[0-9;]*m")
    # Start/keep-active markers (loose: any of these substrings).
    _ACT = re.compile(r"launch_slot|processing task|print_timing|update_slots: id", re.I)
    # Idle marker (loose: matches "all slots are idle" and minor rewordings).
    _IDLE = re.compile(r"slots are idle", re.I)
    # Lines we treat as pure noise (don't count as activity on their own).
    _NOISE = re.compile(r"slots are idle|cache state|prompt cache", re.I)
    # Exact per-request timings.  ms / N tokens -> we compute tok/s ourselves so
    # we don't depend on the "(X tokens per second)" wording.
    _PROMPT_EVAL = re.compile(r"prompt eval time\s*=\s*([\d.]+)\s*ms\s*/\s*(\d+)\s*tokens", re.I)
    _DECODE_EVAL = re.compile(r"(?<!prompt )\beval time\s*=\s*([\d.]+)\s*ms\s*/\s*(\d+)\s*tokens", re.I)
    # Wall-clock total for the request (prefill + generation), logged as its own
    # line: "total time = 13560.52 ms / 345 tokens".
    _TOTAL_EVAL = re.compile(r"total time\s*=\s*([\d.]+)\s*ms\s*/\s*(\d+)\s*tokens", re.I)
    _DRAFT = re.compile(
        r"draft acceptance\s*=\s*([\d.]+)\D+?(\d+)\s*accepted\s*/\s*(\d+)\s*generated", re.I
    )
    _ACCLEN = re.compile(r"mean acceptance length\s*=\s*([\d.]+)", re.I)

    def __init__(self, path: Optional[str]):
        self.path = path
        self.ok = bool(path)
        self._pos = 0
        self.markers_seen = False  # have we ever recognised a known marker?
        self.last: dict = {}       # exact stats from the most recent request
        if path:
            try:
                self._pos = os.path.getsize(path)  # start tailing at the end
            except Exception:
                self.ok = False

    def poll(self) -> Optional[bool]:
        """Read new log content. Returns True if it indicates request activity,
        False if it only contains idle/noise, or None if there was no new
        content (caller should keep its current state)."""
        if not self.path:
            return None
        try:
            size = os.path.getsize(self.path)
        except Exception:
            return None
        if size < self._pos:       # truncated / rotated -> restart from top
            self._pos = 0
        if size == self._pos:
            return None
        try:
            with open(self.path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(self._pos)
                chunk = f.read()
                self._pos = f.tell()
        except Exception:
            return None
        return self._consume(chunk)

    def _consume(self, chunk: str) -> bool:
        saw_activity = False
        for raw in chunk.splitlines():
            line = self._ANSI.sub("", raw).strip()
            if not line:
                continue
            self._parse_stats(line)
            if self._ACT.search(line):
                saw_activity = True
                self.markers_seen = True
            elif self._IDLE.search(line):
                self.markers_seen = True
            elif not self._NOISE.search(line):
                # Unrecognised, non-noise growth -> assume something happened.
                saw_activity = True
        return saw_activity

    def _parse_stats(self, line: str) -> None:
        m = self._PROMPT_EVAL.search(line)
        if m:
            self.last["prefill"] = self._timing(m)
        m = self._DECODE_EVAL.search(line)
        if m:
            self.last["decode"] = self._timing(m)
        m = self._TOTAL_EVAL.search(line)
        if m:
            self.last["total"] = self._timing(m)
        m = self._DRAFT.search(line)
        if m:
            d = self.last.setdefault("draft", {})
            d["rate"] = float(m.group(1))
            d["accepted"] = int(m.group(2))
            d["generated"] = int(m.group(3))
        m = self._ACCLEN.search(line)
        if m:
            self.last.setdefault("draft", {})["mean_len"] = float(m.group(1))

    @staticmethod
    def _timing(m) -> dict:
        ms, toks = float(m.group(1)), int(m.group(2))
        secs = ms / 1000.0
        return {"tokens": toks, "secs": secs, "tps": (toks / secs) if secs > 0 else None}
