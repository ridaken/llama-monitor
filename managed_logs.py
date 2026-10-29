"""Filtered, segmented diagnostics owned by the capture process, not the dashboard."""
from __future__ import annotations

import base64
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid

import store
from collectors import DeviceMemory, _DEVINFO_RE, _SPEC_RE
from placement import LayerPlacement

BUDGET = 50 * 1024 * 1024
SEGMENT = 1024 * 1024
MAX_LINE = 64 * 1024
STARTUP_LIMIT = 1024 * 1024
METADATA_LIMIT = 512 * 1024
READ_LIMIT = 256 * 1024
_ID = re.compile(r"[a-f0-9]{32}\Z")
_IMPORTANT = re.compile(r"processing task|stop processing|print_timing|draft acceptance|mean acceptance|"
                        r"cancel(?:led|ing)? task|request.*(?:failed|completed)", re.I)
_FAILURE = re.compile(r"\b(?:error|fatal|warning|warn|failed|failure|exception|assert(?:ion)?|"
                      r"SIG[A-Z]+|segmentation fault|out of memory|traceback)\b", re.I)
_NOISE = re.compile(r"\bque\s|\bres\s|all slots are idle|waiting for new tasks|"
                    r"all tasks already finished|n_processing_slots|n_idle_slots|"
                    r"update_slots:|process_single_task|process_sing:|"
                    r"(?:GET|POST) /(?:health|props|slots|metrics)\b|"
                    r"n_gen\s*=|sampled token|sampling token|token piece", re.I)
_METADATA = re.compile(r"assigned to device|loaded meta data|n_layer(?:_all)?\s*=|using device|"
                       r"buffer size\s*=|fitting params|device_info:|MiB free|"
                       r"\bbuild\s*[:=]|draft-?mtp|speculative impl|\[spec\]|split[_ -]mode", re.I)
_PAYLOAD = re.compile(r"^(?:.*?\b(?:prompt|response|completion|generated text|token piece)\s*[:=])", re.I)
_READY = re.compile(r"server is listening|model loaded|all slots are idle", re.I)
_PROGRESS = re.compile(r"n_gen\s*=|graphs reused\s*=|n_past\s*=|prompt processing progress|"
                       r"sampled token|sampling token|token piece", re.I)


def root() -> Path:
    return Path(store.MANAGED_LOG).parent / "logs"


def run_dir(run_id: str) -> Path:
    if not _ID.fullmatch(run_id):
        raise ValueError("Invalid log run identifier.")
    path = root() / run_id
    if path.is_symlink() or path.resolve().parent != root().resolve():
        raise ValueError("Invalid log run directory.")
    return path


def load(run_id: str) -> dict:
    if not _ID.fullmatch(run_id):
        return {}  # old history run IDs are not necessarily diagnostic IDs
    try:
        for attempt in range(5):
            try:
                record = json.loads((run_dir(run_id) / "run.json").read_text(encoding="utf-8"))
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.01)  # Windows sharing violations during replacement
        try:
            health = json.loads((run_dir(run_id) / "health.json").read_text(encoding="utf-8"))
            if health.get("error"):
                record["logging_error"] = health["error"]
        except (OSError, ValueError):
            pass
        return record
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def atomic(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=True, separators=(",", ":"), default=list)
    for attempt in range(5):
        try:
            os.replace(temporary, path)
            break
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.01)


def segments(run_id: str) -> list[Path]:
    return sorted(p for p in run_dir(run_id).glob("*.jsonl")
                  if re.fullmatch(r"\d{20}\.jsonl", p.name) and not p.is_symlink())


def _records():
    runs = []
    if root().exists():
        for directory in root().iterdir():
            if _ID.fullmatch(directory.name) and not directory.is_symlink():
                record = load(directory.name)
                if record:
                    runs.append(record)
    runs.sort(key=lambda r: (r["started_at"], r["run_id"]), reverse=True)
    return runs


def list_runs(limit: int = 50, cursor: str = "") -> dict:
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    if cursor and not _ID.fullmatch(cursor):
        raise ValueError("Invalid log listing cursor.")
    runs = _records()
    if cursor:
        index = next((i for i, r in enumerate(runs) if r["run_id"] == cursor), None)
        if index is None:
            raise ValueError("Log listing cursor has expired; reload the list.")
        runs = runs[index + 1:]
    keys = ("run_id", "config_name", "started_at", "ended_at", "state", "stop_reason",
            "exit_code", "gap", "logging_error", "startup_truncated")
    return {"items": [{k: r.get(k) for k in keys} for r in runs[:limit]],
            "next_cursor": runs[limit - 1]["run_id"] if len(runs) > limit else None}


def latest() -> str | None:
    items = list_runs(1)["items"]
    return items[0]["run_id"] if items else None


def read(run_id: str, offset: int = 0, *, tail: bool = False, limit: int = READ_LIMIT) -> tuple[bytes, int, bool]:
    """Offsets are absolute byte positions across immutable, numbered segments."""
    paths = segments(run_id)
    if not paths:
        return b"", offset, False
    first = int(paths[0].stem)
    end = int(paths[-1].stem) + paths[-1].stat().st_size
    gap = offset < first or offset > end
    offset = first if offset > end or offset < 0 else max(first, offset)
    skip_partial = tail and end - offset > limit
    if skip_partial:
        offset = end - limit
    output = bytearray()
    for path in paths:
        start = int(path.stem)
        try:
            with path.open("rb") as source:
                size = os.fstat(source.fileno()).st_size
                if start + size <= offset:
                    continue
                if start > offset:
                    gap = True
                    offset = start
                source.seek(offset - start)
                if skip_partial:
                    offset += len(source.readline())
                    skip_partial = False
                raw = source.read(limit - len(output))
        except FileNotFoundError:
            gap = True
            continue
        endline = raw.rfind(b"\n")
        if endline < 0:
            break
        raw = raw[:endline + 1]
        output.extend(raw)
        offset += len(raw)
        if len(output) >= limit - MAX_LINE:
            break
    return bytes(output), offset, gap


def console(run_id: str, cursor: str = "", offset: int = 0, *, follow_latest: bool = False) -> dict:
    run_dir(run_id)
    record = load(run_id)
    if not record:
        raise FileNotFoundError("Log run is no longer retained.")
    switched = False
    if cursor:
        try:
            selected, position = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if type(position) is not int or position < 0:
                raise ValueError
            if selected != run_id:
                if not follow_latest:
                    raise ValueError
                offset, cursor, switched = 0, "", True
            else:
                offset = position
        except Exception as exc:
            raise ValueError("Invalid log cursor.") from exc
    raw, position, gap = read(run_id, offset, tail=not cursor and offset == 0)
    lines = []
    for line in raw.splitlines():
        try:
            obj = json.loads(line)
            msg = obj.get("msg", "")
        except (ValueError, AttributeError):
            msg = line.decode("utf-8", "replace")
        lines.append(msg.rstrip())
    paths = segments(run_id)
    size = int(paths[-1].stem) + paths[-1].stat().st_size if paths else 0
    return {"available": True, "content": "\n".join(lines) + ("\n" if lines else ""),
            "offset": position, "size": size, "path": str(run_dir(run_id)), "source": "filtered",
            "run_id": run_id, "cursor": base64.urlsafe_b64encode(json.dumps([run_id, position]).encode()).decode(),
            "reset": gap or switched, "gap": gap or record.get("gap", False), "error": record.get("logging_error"),
            "run": {k: record.get(k) for k in ("state", "stop_reason", "exit_code", "startup_truncated")}}


class Filter:
    def __init__(self):
        self.startup = True
        self.startup_bytes = 0
        self.continuation = False

    def select(self, raw: str) -> tuple[dict | None, str]:
        raw = re.sub(r"\x1b\[[0-9;]*m", "", raw).rstrip("\r\n")
        if not raw.strip():
            return None, ""
        obj = None
        try:
            obj = json.loads(raw) if raw.lstrip().startswith("{") else None
        except ValueError:
            pass
        if isinstance(obj, dict) and obj.get("type") == "log" and isinstance(obj.get("msg"), str):
            msg = obj["msg"].rstrip()
            level = str(obj.get("level", "info")).lower()
        elif isinstance(obj, dict):
            return None, ""  # payload and unsupported typed records are not diagnostics
        else:
            msg = raw
            level = "debug" if re.match(r"^(?:[\d:.]+\s+)?D\s", msg) else "info"
            obj = {"type": "log", "level": level, "msg": msg}
        # Never copy obvious payload dumps, even while capturing startup.
        if _PAYLOAD.search(msg) and not _IMPORTANT.search(msg) and not _FAILURE.search(msg):
            self.continuation = False
            return None, msg
        notable = level in ("warn", "warning", "error", "fatal") or bool(_FAILURE.search(msg))
        if _PROGRESS.search(msg) and not notable:
            return None, msg
        continuation = self.continuation and (raw[:1].isspace() or msg.startswith(("  ", "File ", "at ")))
        self.continuation = notable or continuation
        keep = notable or continuation or bool(_IMPORTANT.search(msg)) or bool(_METADATA.search(msg))
        if self.startup:
            self.startup_bytes += len(raw.encode("utf-8"))
            keep |= self.startup_bytes <= STARTUP_LIMIT
            if _READY.search(msg) or self.startup_bytes > STARTUP_LIMIT:
                self.startup = False
        elif not keep:
            keep = level not in ("debug", "trace") and not _NOISE.search(msg)
        if keep:
            obj = {k: obj[k] for k in ("type", "level", "msg", "time") if k in obj}
            obj["msg"] = msg
            obj["observed_at"] = time.time()
            return obj, msg
        return None, msg


class Writer:
    def __init__(self, run_id: str, config_name: str | None = None, *, budget: int = BUDGET,
                 segment_size: int = SEGMENT):
        self.run_id = run_id
        self.directory = run_dir(run_id)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.budget, self.segment_size = budget, segment_size
        self.filter = Filter()
        self.placement, self.memory = LayerPlacement(), DeviceMemory()
        self.baseline = {}
        self.spec = False
        self.position = 0
        self.segment_start = 0
        self.segment_bytes = 0
        self.record = {"run_id": run_id, "config_name": (config_name or "")[:256],
                       "started_at": time.time(), "ended_at": None, "state": "starting",
                       "exit_code": None, "stop_reason": None, "gap": False,
                       "startup_truncated": False, "logging_error": None}
        # Reserve a small error channel before inference starts. Updating existing
        # allocated bytes can report ENOSPC even when atomic replacement cannot.
        (self.directory / "health.json").write_text('{"error":null}'.ljust(4096), encoding="utf-8")
        self.publish()

    def fail(self, message):
        self.record["logging_error"] = message
        self.record["gap"] = True
        try:
            value = json.dumps({"error": message[:256]}, ensure_ascii=True).ljust(4096)
            with (self.directory / "health.json").open("r+", encoding="utf-8") as output:
                output.write(value)
                output.truncate()
        except OSError:
            pass

    def publish(self):
        placement = {**self.placement.__dict__, "current": self.placement.current is not None}
        self.record["metadata"] = {"placement": placement, "memory": self.memory.__dict__,
                                   "baseline": self.baseline, "spec": self.spec}
        if len(json.dumps(self.record["metadata"], default=list)) > METADATA_LIMIT:
            self.record["startup_truncated"] = True
            self.placement, self.memory = LayerPlacement(), DeviceMemory()
            self.baseline = {}
            self.record["metadata"] = {"placement": self.placement.__dict__, "memory": self.memory.__dict__,
                                       "baseline": {}, "spec": self.spec}
        try:
            atomic(self.directory / "run.json", self.record)
        except OSError as exc:
            self.fail(f"Cannot persist diagnostics: {exc}")

    def append(self, obj: dict):
        data = (json.dumps(obj, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
        if len(data) > MAX_LINE:
            obj = {**obj, "msg": obj.get("msg", "")[:MAX_LINE // 12 - 128] + " [log record truncated]"}
            data = (json.dumps(obj, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
            self.record["gap"] = True
        if self.segment_bytes and self.segment_bytes + len(data) > self.segment_size:
            self.segment_start = self.position
            self.segment_bytes = 0
        try:
            with (self.directory / f"{self.segment_start:020d}.jsonl").open("ab") as output:
                output.write(data)
            self.position += len(data)
            self.segment_bytes += len(data)
            self.enforce_budget()
        except OSError as exc:
            self.fail(f"Cannot write diagnostics: {exc}")
            self.publish()

    def event(self, message: str):
        self.append({"type": "lifecycle", "msg": message, "observed_at": time.time()})

    def feed(self, raw: str):
        obj, msg = self.filter.select(raw)
        metadata = bool(_METADATA.search(msg))
        if metadata:
            self.placement.feed(msg)
            self.memory.feed(msg)
            self.baseline.update({label.upper(): float(free) for label, free in _DEVINFO_RE.findall(msg)})
            self.spec |= bool(_SPEC_RE.search(msg))
        if self.filter.startup_bytes > STARTUP_LIMIT and not self.record["startup_truncated"]:
            self.record["startup_truncated"] = True
            self.event("Startup capture truncated; placement metadata is retained separately.")
            metadata = True
        if self.record["state"] == "running" and _READY.search(msg):
            self.record["state"] = "ready"
            self.event("Model ready.")
            metadata = True
        if obj:
            self.append(obj)
        if metadata:
            self.publish()
            self.enforce_budget()

    def enforce_budget(self):
        def size():
            total = 0
            for path in root().rglob("*"):
                try:
                    if path.is_file() and not path.is_symlink():
                        total += path.stat().st_size
                except FileNotFoundError:
                    pass  # a previous supervisor may be finishing retention
            return total
        if size() <= self.budget:
            return
        records = _records()
        for rec in reversed(records):
            if rec["run_id"] == self.run_id or rec["state"] in ("starting", "running", "ready"):
                continue
            directory = run_dir(rec["run_id"])
            # Validate the resolved target before recursively deleting a run.
            if directory.resolve().parent != root().resolve() or directory.is_symlink():
                continue
            shutil.rmtree(directory)
            if size() <= self.budget:
                return
        for path in segments(self.run_id):
            if int(path.stem) == self.segment_start:
                continue
            path.unlink(missing_ok=True)
            self.record["gap"] = True
            self.publish()
            if size() <= self.budget:
                return

    def finish(self, code: int | None, reason: str | None = None):
        try:
            intent = json.loads((self.directory / "stop.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            intent = {}
        self.record.update(state="exited", ended_at=time.time(), exit_code=code,
                           stop_reason=reason or (intent.get("reason") if not intent.get("failed") else None) or "unexpected_exit")
        if intent:
            self.event(f"Stop requested: {intent['reason']}."
                       + (" Escalating to forced termination." if intent.get("forced") else "")
                       + (" Stop failed." if intent.get("failed") else ""))
        self.event(f"Server exited: code {code}; reason {self.record['stop_reason']}."
                   + (" Forced termination was requested." if intent.get("forced") else ""))
        self.publish()
        self.enforce_budget()


def stop_intent(run_id: str | None, reason: str, *, forced: bool = False, failed: bool = False):
    if run_id and load(run_id):
        try:
            atomic(run_dir(run_id) / "stop.json", {"reason": reason, "forced": forced, "failed": failed, "at": time.time()})
        except OSError as exc:
            # Disk failure must not prevent an explicitly requested model stop.
            try:
                message = f"Cannot record stop intent: {exc}"[:256]
                with (run_dir(run_id) / "health.json").open("r+", encoding="utf-8") as output:
                    output.write(json.dumps({"error": message}, ensure_ascii=True).ljust(4096))
                    output.truncate()
            except OSError:
                pass


def reconcile():
    """A dead supervisor cannot supply an exit code after an abrupt interruption."""
    from processes import identity_matches
    for rec in _records():
        if rec["state"] not in ("starting", "running", "ready"):
            continue
        record = load(rec["run_id"])
        identity = record.get("supervisor_identity")
        if not identity:
            if record["state"] == "starting" and time.time() - record["started_at"] > 15:
                record.update(state="interrupted", stop_reason="unclean_interruption", exit_code=None, ended_at=None)
                atomic(run_dir(rec["run_id"]) / "run.json", record)
            continue
        try:
            alive = identity_matches(identity)
        except Exception:
            continue  # permission failure is not proof of an exit
        if not alive:
            try:
                child_alive = bool(record.get("identity") and identity_matches(record["identity"]))
            except Exception:
                continue
            if child_alive:
                record["logging_error"] = "Capture process exited while the server is still running."
            else:
                record.update(state="interrupted", stop_reason="unclean_interruption", exit_code=None, ended_at=None)
            atomic(run_dir(rec["run_id"]) / "run.json", record)


def import_legacy():
    paths = [Path(store.MANAGED_LOG), Path(store.MANAGED_LOG + ".startup")]
    existing = [p for p in paths if p.is_file() and not p.is_symlink() and p.stat().st_size]
    if not existing:
        return
    writer = Writer(uuid.uuid4().hex, "Previous installation logs")
    recent = deque(maxlen=4096)
    for source in existing:
        writer.event(f"Importing {source.name}.")
        with source.open("rb") as stream:
            for raw, truncated in bounded_lines(stream):
                signature = hashlib.sha256(raw).digest()
                if signature not in recent:
                    writer.feed(raw.decode("utf-8", "replace"))
                    recent.append(signature)
                if truncated:
                    writer.event("Oversized legacy log line truncated.")
    writer.finish(None, "legacy_import")
    for path in paths:
        path.unlink(missing_ok=True)


def bounded_lines(stream):
    """Read through EOF with bounded memory, retaining a prefix of oversized lines."""
    while True:
        raw = stream.readline(MAX_LINE + 1)
        if not raw:
            return
        truncated = len(raw) > MAX_LINE
        prefix = raw[:MAX_LINE]
        while not raw.endswith(b"\n") and len(raw) > MAX_LINE:
            raw = stream.readline(MAX_LINE + 1)
        yield prefix, truncated
