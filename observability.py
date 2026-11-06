"""Local application events and measured usage, independent of request history.

Each producer owns a session and writes cumulative daily snapshots. A checkpoint
and its detail are committed together; retries cannot increment a total twice.
All writes run off the producer thread, including detached model supervisors.
"""
from __future__ import annotations

import base64
from collections import defaultdict
from contextlib import contextmanager
import hashlib
import json
import logging
import math
import os
import queue
import sqlite3
import threading
import time
import uuid

import store

BUDGET = 50 * 1024 * 1024
LEVELS = {"info": 20, "warning": 30, "error": 40}
STATES = ("loading", "busy", "idle", "unknown")
TABLES = ("app_events", "usage_intervals", "app_sessions", "model_sessions", "usage_daily")
CONTEXT = {"app_session_id", "run_id", "operation_id", "model", "role", "config_name",
           "exit_code", "reason", "exception_type", "suppressed_count", "forced", "state"}
DEFINITIONS = {
    "app_uptime_seconds": "Observed backend lifetime, through the last checkpoint.",
    "initialization_seconds": "Backend main entry to completed application initialization.",
    "process_uptime_seconds": "Observed model child lifetime, including loading.",
    "load_seconds": "Child spawn to first ready log marker; not HTTP-verified readiness.",
    "loading_seconds": "Observed time before the first ready log marker, including failed loads.",
    "busy_seconds": "Wall time with one or more observed active tasks; overlaps count once.",
    "idle_seconds": "Post-readiness time in an explicitly known idle state.",
    "unknown_seconds": "Observed model lifetime without a reliable activity classification.",
    "utilization": "busy_seconds / (busy_seconds + idle_seconds); unknown time excluded.",
    "stop_seconds": "Stop intent to observed exit; overlaps model lifetime.",
    "restoration_seconds": "Gaming restoration start to completion or failure.",
    "launch_attempts": "Supervisor attempts to create a model child; preflight rejections excluded.",
    "ready_outcomes": "Managed children that emitted their first recognized ready marker.",
    "pre_ready_failures": "Model attempts that ended without a ready log marker.",
    "unexpected_exits": "Observed child exits without a recorded stop intent.",
    "restart_attempts": "Explicit restart control requests, including rejected requests.",
    "stop_failures": "Stop attempts reported as failed by the process owner.",
    "cpu_fallback_attempts": "Embedding lifecycle transitions into CPU retry.",
    "cpu_fallback_outcomes": "CPU fallback attempts that reached verified ready state.",
    "stop_operations": "Recorded stop intents followed by an observed child exit.",
    "restoration_operations": "Observed restoration attempts that left the restoring phase.",
    "restoration_failures": "Restoration attempts ending in failure or retry countdown.",
    "interruptions": "Producer deaths detected without a final checkpoint.",
}


def day_start(value):
    return math.floor(value / 86400) * 86400.0


def model_key(path):
    return os.path.normcase(os.path.abspath(os.path.expanduser(path))) if path else ""


def bounds(from_ts=None, to_ts=None):
    end = time.time() if to_ts is None else to_ts
    if isinstance(end, bool) or not isinstance(end, (int, float)) or not math.isfinite(end) or end < 0:
        raise ValueError("date bounds must be finite, nonnegative, and increasing")
    start = day_start(end) if from_ts is None else from_ts
    if any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n)
           for n in (start, end)) or start < 0 or end <= start:
        raise ValueError("date bounds must be finite, nonnegative, and increasing")
    return start, end


def _safe_context(context):
    # Callers supply fixed messages, never raw exception messages or request data.
    return {key: value[:1024] if isinstance(value, str) else value
            for key, value in context.items() if key in CONTEXT
            and (value is None or type(value) in (str, int, float, bool))}


class Storage:
    def __init__(self, path, budget=BUDGET):
        self.path, self.budget = path, budget
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            for table in ("app_sessions", "model_sessions"):
                db.execute(f"CREATE TABLE IF NOT EXISTS {table} ("
                           "id TEXT PRIMARY KEY, as_of REAL NOT NULL, active INTEGER NOT NULL, "
                           "seq INTEGER NOT NULL, data TEXT NOT NULL, bytes INTEGER NOT NULL)")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS app_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
                    timestamp REAL NOT NULL, level INTEGER NOT NULL, component TEXT NOT NULL,
                    code TEXT NOT NULL, model TEXT, role TEXT, run_id TEXT,
                    data TEXT NOT NULL, bytes INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS app_events_time ON app_events(timestamp, id);
                CREATE INDEX IF NOT EXISTS app_events_run ON app_events(run_id, id);
                CREATE TABLE IF NOT EXISTS usage_intervals (
                    producer TEXT NOT NULL, seq INTEGER NOT NULL, ordinal INTEGER NOT NULL,
                    kind TEXT NOT NULL, model TEXT NOT NULL, role TEXT NOT NULL,
                    start REAL NOT NULL, end REAL NOT NULL, state TEXT NOT NULL,
                    seconds REAL NOT NULL, bytes INTEGER NOT NULL,
                    PRIMARY KEY(producer, seq, ordinal));
                CREATE INDEX IF NOT EXISTS usage_interval_time ON usage_intervals(start, end);
                CREATE TABLE IF NOT EXISTS usage_daily (
                    producer TEXT NOT NULL, day REAL NOT NULL, kind TEXT NOT NULL,
                    model TEXT NOT NULL, role TEXT NOT NULL, run_id TEXT NOT NULL DEFAULT '', data TEXT NOT NULL,
                    bytes INTEGER NOT NULL, PRIMARY KEY(producer, day));
                CREATE INDEX IF NOT EXISTS usage_daily_day ON usage_daily(day);
                CREATE TABLE IF NOT EXISTS observability_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT OR IGNORE INTO observability_meta VALUES('schema_version', '1');
                INSERT OR IGNORE INTO observability_meta VALUES('pruned_rows', '0');
                INSERT OR IGNORE INTO observability_meta VALUES('event_floor', '0');
                INSERT OR IGNORE INTO observability_meta VALUES('daily_floor', '0');
                INSERT OR IGNORE INTO observability_meta VALUES('detail_floor', '0');
            """)
            if "run_id" not in {r["name"] for r in db.execute("PRAGMA table_info(usage_daily)")}:
                db.execute("ALTER TABLE usage_daily ADD COLUMN run_id TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=0.1)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _meta(db, key):
        row = db.execute("SELECT value FROM observability_meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _set_meta(db, key, value):
        db.execute("INSERT OR REPLACE INTO observability_meta VALUES(?,?)", (key, json.dumps(value)))

    def write(self, item):
        with self.connect() as db:
            if item.get("detail_gap_until"):
                self._set_meta(db, "detail_floor", max(self._meta(db, "detail_floor"), item["detail_gap_until"]))
            if item["type"] == "event":
                data = json.dumps(item["data"], ensure_ascii=True, separators=(",", ":"))
                row = item["data"]
                db.execute("INSERT OR IGNORE INTO app_events(event_id,timestamp,level,component,code,"
                           "model,role,run_id,data,bytes) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (row["event_id"], row["timestamp"], LEVELS[row["level"]], row["component"],
                            row["code"], row.get("model"), row.get("role"), row.get("run_id"), data, len(data) + 256))
            else:
                data = item["data"]
                table = "model_sessions" if data["kind"] == "model" else "app_sessions"
                previous = db.execute(f"SELECT seq FROM {table} WHERE id=?", (data["id"],)).fetchone()
                if previous and previous[0] >= data["seq"]:
                    return
                encoded = json.dumps(data, separators=(",", ":"))
                db.execute(f"INSERT OR REPLACE INTO {table} VALUES(?,?,?,?,?,?)",
                           (data["id"], data["as_of"], data["active"], data["seq"], encoded, len(encoded) + 256))
                for ordinal, interval in enumerate(item["intervals"]):
                    charged = len(json.dumps([data["id"], data["kind"], data["model"], data["role"], interval])) + 256
                    db.execute("INSERT OR IGNORE INTO usage_intervals VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                               (data["id"], data["seq"], ordinal, data["kind"], data["model"], data["role"],
                                *interval, charged))
                floor = self._meta(db, "daily_floor")
                for day, values in item["daily"].items():
                    if float(day) < floor:
                        continue
                    encoded = json.dumps(values, separators=(",", ":"))
                    db.execute("INSERT OR REPLACE INTO usage_daily(producer,day,kind,model,role,run_id,data,bytes) VALUES(?,?,?,?,?,?,?,?)",
                               (data["id"], float(day), data["kind"], data["model"], data["role"],
                                data.get("run_id") or (data["id"] if data["kind"] == "model" else ""),
                                encoded, len(encoded) + len(data["model"]) + 256))

    def prune(self):
        with self.connect() as db:
            size = sum(db.execute(f"SELECT COALESCE(SUM(bytes),0) FROM {t}").fetchone()[0] for t in TABLES)
            if size <= self.budget:
                return
            removed = 0
            for table, predicate, order in (
                ("app_events", "1", "id"),
                ("usage_intervals", "1", "end"),
                ("app_sessions", "active=0", "as_of"),
                ("model_sessions", "active=0", "as_of"),
                ("usage_daily", "day < ?", "day"),
            ):
                params = (day_start(time.time()),) if table == "usage_daily" else ()
                while size > self.budget * .9:
                    rows = db.execute(f"SELECT rowid AS _rowid, * FROM {table} WHERE {predicate} ORDER BY {order} LIMIT 100",
                                      params).fetchall()
                    if not rows:
                        break
                    for row in rows:
                        if size <= self.budget * .9:
                            break
                        if table == "app_events":
                            self._set_meta(db, "event_floor", max(self._meta(db, "event_floor"), row["id"]))
                        elif table == "usage_daily":
                            # Evict complete UTC buckets together, never a partial collection of producers.
                            bucket = db.execute("SELECT rowid,bytes FROM usage_daily WHERE day=?", (row["day"],)).fetchall()
                            size -= sum(r["bytes"] for r in bucket)
                            removed += len(bucket)
                            db.execute("DELETE FROM usage_daily WHERE day=?", (row["day"],))
                            self._set_meta(db, "daily_floor", row["day"] + 86400)
                            break
                        elif table == "usage_intervals":
                            self._set_meta(db, "detail_floor", max(self._meta(db, "detail_floor"), row["end"]))
                        elif table in ("app_sessions", "model_sessions"):
                            self._set_meta(db, "detail_floor", max(self._meta(db, "detail_floor"), row["as_of"]))
                        db.execute(f"DELETE FROM {table} WHERE rowid=?", (row["_rowid"],))
                        size -= row["bytes"]
                        removed += 1
            self._set_meta(db, "pruned_rows", self._meta(db, "pruned_rows") + removed)

    def status(self):
        with self.connect() as db:
            meta = {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT * FROM observability_meta")}
            meta["estimated_retained_bytes"] = sum(
                db.execute(f"SELECT COALESCE(SUM(bytes),0) FROM {t}").fetchone()[0] for t in TABLES)
            meta["budget_bytes"] = self.budget
            meta["oldest_event_at"] = db.execute("SELECT MIN(timestamp) FROM app_events").fetchone()[0]
            meta["oldest_daily_at"] = db.execute("SELECT MIN(day) FROM usage_daily").fetchone()[0]
            meta["producers"] = [json.loads(r[0]) for t in ("app_sessions", "model_sessions")
                                 for r in db.execute(f"SELECT data FROM {t} WHERE active=1")]
        return meta

    def events(self, *, level="info", component="", model="", role="", run_id="", from_ts=None,
               to_ts=None, cursor=None, limit=50, direction="backward"):
        if level not in LEVELS or direction not in ("forward", "backward") or not 1 <= limit <= 200:
            raise ValueError("invalid level, direction, or limit")
        _validate_role(role)
        start, end = bounds(0 if from_ts is None else from_ts, to_ts)
        signature = hashlib.sha256(json.dumps([level, component, model, role, run_id, from_ts, to_ts, direction]).encode()).hexdigest()
        position = None
        if cursor:
            try:
                signature_in, position = json.loads(base64.urlsafe_b64decode(cursor))
                if signature_in != signature or type(position) is not int or position < 0:
                    raise ValueError
            except Exception as exc:
                raise ValueError("invalid or incompatible event cursor") from exc
        clauses, values = ["level>=?", "timestamp>=?", "timestamp<?"], [LEVELS[level], start, end]
        for key, value in (("component", component), ("model", model), ("role", role), ("run_id", run_id)):
            if value:
                clauses.append(f"{key}=?")
                values.append(value)
        if position is not None:
            clauses.append("id>?" if direction == "forward" else "id<?")
            values.append(position)
        with self.connect() as db:
            floor = self._meta(db, "event_floor")
            rows = db.execute("SELECT id,data FROM app_events WHERE " + " AND ".join(clauses)
                              + (" ORDER BY id ASC" if direction == "forward" else " ORDER BY id DESC")
                              + " LIMIT ?", (*values, limit + 1)).fetchall()
        page = rows[:limit]
        last = page[-1]["id"] if page else max(position or 0, floor)
        token = base64.urlsafe_b64encode(json.dumps([signature, last]).encode()).decode()
        return {"items": [{**json.loads(r["data"]), "id": r["id"]} for r in page],
                "next_cursor": token if direction == "forward" or len(rows) > limit else None,
                "has_more": len(rows) > limit, "gap": bool(floor and (position is None or position < floor))}

    def metrics(self, *, model="", role="", run_id="", from_ts=None, to_ts=None):
        _validate_role(role)
        start, end = bounds(from_ts, to_ts)
        with self.connect() as db:
            sessions = [json.loads(r[0]) for t in ("app_sessions", "model_sessions")
                        for r in db.execute(f"SELECT data FROM {t}")]
            by_id = {s["id"]: s for s in sessions}
            if run_id and not any(s["id"] == run_id and s["kind"] == "model" for s in sessions):
                return {"available": False, "reason": "Run detail unavailable or expired.", "run_id": run_id}
            def match(kind, row_model, row_role, producer, associated_run=""):
                return ((not model or row_model == model) and (not role or row_role == role)
                        and (not run_id or producer == run_id or associated_run == run_id
                             or by_id.get(producer, {}).get("run_id") == run_id))
            totals = defaultdict(float)
            model_totals = defaultdict(lambda: defaultdict(float))
            def add(key, value, kind, row_model, row_role):
                totals[key] += value
                if kind == "model" or row_model:
                    model_totals[row_model, row_role][key] += value
            daily = []
            floor = self._meta(db, "detail_floor")
            # Detail before the global pruning boundary is conservatively aggregated.
            detail_start = max(start, math.ceil(floor / 86400) * 86400 if floor else start)
            for row in db.execute("SELECT * FROM usage_daily WHERE day<? AND day+86400>? ORDER BY day", (end, start)):
                if not match(row["kind"], row["model"], row["role"], row["producer"], row["run_id"]):
                    continue
                values = json.loads(row["data"])
                coarse = row["day"] < detail_start
                daily.append({"day": row["day"], "end": row["day"] + 86400, "kind": row["kind"],
                              "model": row["model"], "role": row["role"], "values": values,
                              "granularity": "daily", "partial_overlap": start > row["day"] or end < row["day"] + 86400})
                # Event-like measurements have timestamps in retained session points.
                # Only use the daily values for whole expired-detail buckets.
                if coarse and start <= row["day"] and end >= row["day"] + 86400:
                    for key, value in values.items():
                        add(key, value, row["kind"], row["model"], row["role"])
            for row in db.execute("SELECT * FROM usage_intervals WHERE end>? AND start<?", (detail_start, end)):
                if not match(row["kind"], row["model"], row["role"], row["producer"]):
                    continue
                length = row["end"] - row["start"]
                fraction = max(0, min(end, row["end"]) - max(detail_start, row["start"])) / length if length > 0 else 0
                key = "app_uptime_seconds" if row["kind"] == "app" else row["state"] + "_seconds"
                add(key, row["seconds"] * fraction, row["kind"], row["model"], row["role"])
                if row["kind"] == "model":
                    add("process_uptime_seconds", row["seconds"] * fraction, row["kind"], row["model"], row["role"])
            for session in sessions:
                if not match(session["kind"], session["model"], session["role"], session["id"]):
                    continue
                for point in session.get("points", []):
                    if detail_start <= point["at"] < end:
                        add(point["metric"], point["value"], session["kind"], session["model"], session["role"])
            known = totals["busy_seconds"] + totals["idle_seconds"]
            for state in STATES:
                totals.setdefault(state + "_seconds", 0.0)
            totals["utilization"] = totals["busy_seconds"] / known if known else None
            models = []
            for (key, model_role), values in sorted(model_totals.items()):
                known = values["busy_seconds"] + values["idle_seconds"]
                values["utilization"] = values["busy_seconds"] / known if known else None
                models.append({"model": key, "model_name": os.path.basename(key), "role": model_role, "totals": dict(values)})
            retained = [s for s in sessions if s["kind"] in ("app", "model")
                        and match(s["kind"], s["model"], s["role"], s["id"])
                        and ((s["started_at"] < end and s["as_of"] >= start)
                             or any(start <= p["at"] < end for p in s.get("points", [])))]
        return {"available": True, "from_ts": start, "to_ts": end, "as_of": time.time(),
                "totals": dict(totals), "models": models, "daily": daily, "sessions": retained,
                "coverage": {"detail_from": detail_start, "daily_before": detail_start if floor else None,
                             "partial_daily_buckets_excluded_from_totals": any(d["partial_overlap"] and d["day"] < detail_start for d in daily),
                             "unknown_seconds": totals["unknown_seconds"]},
                "units": "seconds; counters are counts; utilization is a fraction",
                "definitions": DEFINITIONS, "retention": self.status()}


def _validate_role(role):
    if role and role not in ("primary", "embeddings"):
        raise ValueError("role must be primary or embeddings")


class Recorder:
    """Bounded best-effort producer. No SQLite calls on a model's output thread."""
    def __init__(self, path=None, *, budget=BUDGET, queue_size=1024, autostart=True):
        self.storage = None
        self.error = None
        self.dropped = self.write_failures = 0
        self.last_success = None
        self.detail_gap_until = 0
        self.pending = queue.Queue(maxsize=queue_size)
        self.stop = threading.Event()
        self.thread = None
        self.tracker = None
        self.context = {}
        self.conditions = {}
        self.condition_lock = threading.RLock()
        try:
            self.storage = Storage(path or os.path.join(store.HOME_DIR, "history.sqlite"), budget)
            if autostart:
                self.start()
        except Exception as exc:
            self._failure(exc)

    def start(self):
        if self.storage and self.thread is None:
            self.thread = threading.Thread(target=self._loop, name="observability-writer", daemon=True)
            self.thread.start()

    def _failure(self, exc):
        self.write_failures += 1
        if self.error is None:
            logging.getLogger("llama_monitor.observability").warning("Observability persistence unavailable (%s).", type(exc).__name__)
        self.error = type(exc).__name__

    def submit(self, item):
        if not self.storage or self.stop.is_set():
            self.dropped += 1
            return
        try:
            self.pending.put_nowait(item)
        except queue.Full:
            self.dropped += 1
            self._detail_gap(item)

    def _detail_gap(self, item):
        self.detail_gap_until = max(self.detail_gap_until, max((r[1] for r in item.get("intervals", [])), default=0))

    def event(self, component, code, message, level="info", **context):
        if level not in LEVELS:
            raise ValueError("invalid event level")
        data = {"event_id": uuid.uuid4().hex, "timestamp": time.time(), "level": level,
                "component": component[:64], "code": code[:96], "message": message[:512],
                **_safe_context({**self.context, **context})}
        # ASCII encoding has a bounded six bytes per character; context is bounded too.
        while len(json.dumps(data, ensure_ascii=True).encode()) > 8192:
            longest = max((k for k, v in data.items() if isinstance(v, str) and len(v) > 32),
                          key=lambda k: len(data[k]), default=None)
            if longest is None:
                break
            data[longest] = data[longest][:len(data[longest]) // 2]
        self.submit({"type": "event", "data": data})

    def condition(self, component, code, failed):
        with self.condition_lock:
            count = self.conditions.get((component, code), 0)
            if failed:
                if not count:
                    self.event(component, code, "Background operation failed.", "warning")
                self.conditions[component, code] = count + 1
            elif count:
                self.event(component, code + "_recovered", "Background operation recovered.", suppressed_count=max(0, count - 1))
                del self.conditions[component, code]

    def measure(self, metric, value=1, *, model="", role="", run_id=""):
        """A timestamped operation measurement; no invented model lifetime."""
        at = time.time()
        data = {"id": "operation:" + uuid.uuid4().hex, "kind": "operation", "model": model,
                "role": role, "run_id": run_id, "as_of": at, "started_at": at, "active": False,
                "seq": 1, "points": [{"metric": metric, "value": value, "at": at}]}
        self.submit({"type": "checkpoint", "data": data, "intervals": [],
                     "daily": {day_start(at): {metric: value}}})

    def health(self):
        return {"available": self.storage is not None, "error": self.error,
                "queue_drops": self.dropped, "write_failures": self.write_failures,
                "last_successful_persistence": self.last_success, "queued": self.pending.qsize()}

    def _loop(self):
        last_checkpoint = time.monotonic()
        last_prune = last_checkpoint
        while not self.stop.is_set() or not self.pending.empty():
            try:
                item = self.pending.get(timeout=.2)
            except queue.Empty:
                item = None
            if item is not None:
                for attempt in range(3):
                    try:
                        item["detail_gap_until"] = self.detail_gap_until
                        self.storage.write(item)
                        recovered = self.error is not None
                        self.error = None
                        if recovered:
                            self.event("observability", "persistence_recovered", "Observability persistence recovered.")
                        self.last_success = time.time()
                        break
                    except Exception as exc:
                        self._failure(exc)
                        if attempt == 2:
                            self.dropped += 1
                            self._detail_gap(item)
                        elif self.stop.wait(.05):
                            self.dropped += 1
                            self._detail_gap(item)
                            break
                self.pending.task_done()
            now = time.monotonic()
            if self.tracker and now - last_checkpoint >= 5 and not self.stop.is_set():
                self.tracker.checkpoint()
                last_checkpoint = now
            if now - last_prune >= 5 or (self.stop.is_set() and self.pending.empty()):
                try:
                    reconcile(self.storage)
                    self.storage.prune()
                except Exception as exc:
                    self._failure(exc)
                last_prune = now

    def close(self):
        for (component, code), count in list(self.conditions.items()):
            self.event(component, code + "_suppressed", "Repeated background failures suppressed.",
                       "warning", suppressed_count=max(0, count - 1))
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)


class Tracker:
    def __init__(self, recorder, identifier, *, kind="model", model="", role="primary",
                 config_name="", identity=None, clock=time.monotonic, wall=time.time):
        self.recorder, self.clock, self.wall = recorder, clock, wall
        self.lock = threading.RLock()
        self.last = clock()
        self.at = wall()
        self.state = "loading" if kind == "model" else "running"
        self.active_tasks = {}
        self.uncertain_tasks = False
        self.ready = False
        self.daily = defaultdict(lambda: defaultdict(float))
        self.intervals = []
        self.data = {"id": identifier, "kind": kind, "model": model, "role": role if kind == "model" else "",
                     "model_name": os.path.basename(model),
                     "config_name": config_name, "started_at": self.at, "as_of": self.at,
                     "active": True, "seq": 0, "state": self.state, "points": [],
                     "identity": identity, "outcome": "running"}
        self.started = self.last
        self.stopping = None
        recorder.tracker = self
        self.checkpoint()

    def _advance(self):
        now = self.clock()
        duration = max(0, now - self.last)
        wall = self.wall()
        end = self.at + duration
        key = "app_uptime_seconds" if self.data["kind"] == "app" else self.state + "_seconds"
        cursor = self.at
        while cursor < end:
            boundary = min(end, day_start(cursor) + 86400)
            seconds = boundary - cursor
            self.daily[day_start(cursor)][key] += seconds
            if self.data["kind"] == "model":
                self.daily[day_start(cursor)]["process_uptime_seconds"] += seconds
            self.intervals.append((cursor, boundary, self.state, seconds))
            cursor = boundary
        if abs(wall - end) > 2:
            self.recorder.event("observability", "clock_discontinuity", "Wall clock changed; elapsed time uses a monotonic clock.",
                                run_id=self.data["id"] if self.data["kind"] == "model" else None)
        self.last, self.at = now, wall
        self.data["as_of"] = wall

    def point(self, metric, value=1):
        with self.lock:
            at = self.wall()
            self.daily[day_start(at)][metric] += value
            self.data["points"].append({"metric": metric, "value": value, "at": at})

    def checkpoint(self):
        with self.lock:
            if not self.data["active"]:
                return
            self._advance()
            self.data.update(seq=self.data["seq"] + 1, state=self.state, health=self.recorder.health())
            self.recorder.submit({"type": "checkpoint", "data": json.loads(json.dumps(self.data)),
                                  "intervals": self.intervals,
                                  "daily": {day: dict(values) for day, values in self.daily.items()}})
            self.intervals = []

    def transition(self, state):
        with self.lock:
            if state != self.state:
                self._advance()
                self.state = state
                self.checkpoint()

    def feed(self, message):
        """Only recognized server records, never payload text, reach this parser."""
        from managed_logs import _READY
        from history import LogFollower
        from collectors import LogTailer
        with self.lock:
            if not self.ready and _READY.search(message):
                self.ready = True
                self.point("ready_outcomes")
                self.point("load_seconds", max(0, self.clock() - self.started))
                self.transition("busy" if self.active_tasks else "idle" if LogTailer._IDLE.search(message) else "unknown")
                self.recorder.event("launcher", "model_ready", "Model emitted a ready log marker.",
                                    model=self.data["model"], role=self.data["role"], run_id=self.data["id"])
            if LogTailer._IDLE.search(message):
                self.active_tasks.clear()
                self.uncertain_tasks = False
                if self.ready:
                    self.transition("idle")
                return
            slot = LogFollower._SLOT.search(message)
            if "processing task" in message.lower() and "stop processing" not in message.lower():
                if not slot:
                    self.uncertain_tasks = True
                    self.transition("unknown")
                    return
                task = LogFollower._TASK.search(message)
                self.active_tasks[int(slot[1])] = int(task[1]) if task else None
                if self.ready:
                    self.transition("busy")
            elif "stop processing" in message.lower():
                if not slot or int(slot[1]) not in self.active_tasks:
                    self.uncertain_tasks = True
                    if self.ready:
                        self.transition("unknown")
                    return
                task = LogFollower._TASK.search(message)
                if task and self.active_tasks[int(slot[1])] not in (None, int(task[1])):
                    self.transition("unknown")
                    self.active_tasks.clear()
                    self.uncertain_tasks = True
                    return
                del self.active_tasks[int(slot[1])]
                if self.ready:
                    self.transition("busy" if self.active_tasks else "unknown" if self.uncertain_tasks else "idle")

    def gap(self):
        with self.lock:
            self.active_tasks.clear()
            self.uncertain_tasks = True
            if self.ready:
                self.transition("unknown")

    def stop_intent(self, failed=False, forced=False, requested_at=None):
        with self.lock:
            if self.stopping is None:
                now = self.clock()
                self.stopping = requested_at if type(requested_at) in (int, float) and math.isfinite(requested_at) and self.started <= requested_at <= now else now
            if failed:
                self.point("stop_failures")
            self.recorder.event("launcher", "stop_failed" if failed else "stop_requested", "Managed model stop intent recorded.",
                                "warning" if failed or forced else "info", forced=forced)
            self.checkpoint()

    def finish(self, outcome, exit_code=None):
        with self.lock:
            if not self.data["active"]:
                return
            if self.data["kind"] == "model":
                if not self.ready:
                    self.point("pre_ready_failures")
                if outcome == "unexpected_exit":
                    self.point("unexpected_exits")
                if self.stopping is not None:
                    self.point("stop_operations")
                    self.point("stop_seconds", max(0, self.clock() - self.stopping))
            self._advance()
            self.data.update(outcome=outcome, ended_at=self.wall(), exit_code=exit_code)
            self.checkpoint()
            self.data["active"] = False
            self.data["seq"] += 1
            self.recorder.submit({"type": "checkpoint", "data": json.loads(json.dumps(self.data)),
                                  "intervals": [], "daily": {day: dict(v) for day, v in self.daily.items()}})


def reconcile(storage):
    """Do not manufacture durations or exit timestamps for dead producers."""
    from processes import identity_matches
    with storage.connect() as db:
        for table in ("app_sessions", "model_sessions"):
            for row in db.execute(f"SELECT * FROM {table} WHERE active=1").fetchall():
                data = json.loads(row["data"])
                if not data.get("identity"):
                    continue
                try:
                    if identity_matches(data["identity"]):
                        continue
                except Exception:
                    continue
                # The producer may have published its final checkpoint during
                # identity probing. Its last write is stable once it is dead.
                latest = db.execute(f"SELECT data,active FROM {table} WHERE id=?", (data["id"],)).fetchone()
                if not latest or not latest["active"]:
                    continue
                data = json.loads(latest["data"])
                detected_at = time.time()
                data.update(active=False, outcome="interrupted", unobserved_after=data["as_of"], interruption_detected_at=detected_at)
                data.setdefault("points", []).append({"metric": "interruptions", "at": detected_at, "value": 1})
                day = day_start(detected_at)
                daily = db.execute("SELECT data FROM usage_daily WHERE producer=? AND day=?", (data["id"], day)).fetchone()
                values = json.loads(daily[0]) if daily else {}
                values["interruptions"] = values.get("interruptions", 0) + 1
                payload = json.dumps(values)
                db.execute("INSERT OR REPLACE INTO usage_daily(producer,day,kind,model,role,run_id,data,bytes) VALUES(?,?,?,?,?,?,?,?)",
                           (data["id"], day, data["kind"], data["model"], data["role"],
                            data["id"] if data["kind"] == "model" else "", payload, len(payload) + len(data["model"]) + 256))
                encoded = json.dumps(data)
                db.execute(f"UPDATE {table} SET active=0,data=?,bytes=? WHERE id=?",
                           (encoded, len(encoded) + 256, row["id"]))
