"""Durable, local history of generations observed in llama-server logs.

The server log supplies timings. Optional native prompt files supply text;
llama-server does not put a task ID in those files, so uncertain matches stay
as separate prompt-only activity entries.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Iterator, Optional

import store
from collectors import LogTailer, _BUF_RE, _DEVINFO_RE, _SPEC_RE, _norm_device


def decode_log_line(raw: str) -> Optional[str]:
    """Return a human-readable message from a text or JSONL log line."""
    line = raw.strip()
    if not line:
        return None
    if line.startswith("{"):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return None  # a damaged JSONL record must not masquerade as activity
        if isinstance(record, dict):
            if record.get("type") == "log" and isinstance(record.get("msg"), str):
                return LogTailer._ANSI.sub("", record["msg"]).strip()
            return None  # future typed records have no supported history schema yet
    return LogTailer._ANSI.sub("", line).strip()


class HistoryDB:
    SORTS = {
        "time": "observed_at",
        "duration": "COALESCE(total_seconds, -1)",
        "prompt_tokens": "COALESCE(prompt_tokens, -1)",
        "generated_tokens": "COALESCE(generated_tokens, -1)",
    }

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    model TEXT,
                    started_at REAL NOT NULL,
                    prompts_cleared_at REAL
                );
                CREATE TABLE IF NOT EXISTS generations (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    task_id INTEGER,
                    slot_id INTEGER,
                    model TEXT,
                    observed_at REAL NOT NULL,
                    completed_at REAL,
                    state TEXT NOT NULL,
                    prompt_tokens INTEGER,
                    generated_tokens INTEGER,
                    prompt_seconds REAL,
                    decode_seconds REAL,
                    total_seconds REAL,
                    draft_accepted INTEGER,
                    draft_generated INTEGER,
                    draft_accept_rate REAL,
                    draft_mean_len REAL
                );
                CREATE TABLE IF NOT EXISTS cursors (
                    path TEXT PRIMARY KEY,
                    file_key TEXT NOT NULL,
                    offset INTEGER NOT NULL,
                    run_id TEXT NOT NULL,
                    gap INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS prompt_files (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    path TEXT NOT NULL,
                    captured_at REAL NOT NULL,
                    prompt_text TEXT,
                    truncated INTEGER NOT NULL DEFAULT 0,
                    cleared INTEGER NOT NULL DEFAULT 0,
                    generation_id TEXT REFERENCES generations(id),
                    UNIQUE(run_id, path)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS prompt_generation ON prompt_files(generation_id)
                    WHERE generation_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS prompt_run_time ON prompt_files(run_id, captured_at);
                CREATE INDEX IF NOT EXISTS generations_time ON generations(observed_at, id);
                CREATE INDEX IF NOT EXISTS generations_model_time ON generations(model, observed_at);
                CREATE INDEX IF NOT EXISTS generations_state_time ON generations(state, observed_at);
                CREATE INDEX IF NOT EXISTS generations_duration ON generations(total_seconds, id);
                CREATE INDEX IF NOT EXISTS generations_prompt ON generations(prompt_tokens, id);
                CREATE INDEX IF NOT EXISTS generations_generated ON generations(generated_tokens, id);
                CREATE INDEX IF NOT EXISTS generations_run_slot ON generations(run_id, slot_id, observed_at);
            """)
            if "gap" not in {row["name"] for row in db.execute("PRAGMA table_info(cursors)")}:
                db.execute("ALTER TABLE cursors ADD COLUMN gap INTEGER NOT NULL DEFAULT 0")
            if "prompts_cleared_at" not in {row["name"] for row in db.execute("PRAGMA table_info(runs)")}:
                db.execute("ALTER TABLE runs ADD COLUMN prompts_cleared_at REAL")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def get_cursor(self, path: str) -> Optional[dict]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM cursors WHERE path = ?", (path,)).fetchone()
            return dict(row) if row else None

    def ensure_run(self, run_id: str, source: str, started_at: float) -> None:
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO runs(id, source, started_at) VALUES(?, ?, ?)",
                       (run_id, source, started_at))

    def run_started_at(self, run_id: str) -> Optional[float]:
        with self.connect() as db:
            row = db.execute("SELECT started_at FROM runs WHERE id = ?", (run_id,)).fetchone()
            return row[0] if row else None

    def prompt_clear_at(self, run_id: str) -> Optional[float]:
        with self.connect() as db:
            row = db.execute("SELECT prompts_cleared_at FROM runs WHERE id = ?", (run_id,)).fetchone()
            return row[0] if row else None

    def set_model(self, run_id: str, model: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE runs SET model = ? WHERE id = ?", (model, run_id))
            db.execute("UPDATE generations SET model = ? WHERE run_id = ? AND model IS NULL",
                       (model, run_id))

    def save_batch(self, path: str, file_key: str, offset: int,
                   run_id: str, rows: list[dict], gap: bool = False) -> None:
        columns = (
            "id", "run_id", "task_id", "slot_id", "model", "observed_at",
            "completed_at", "state", "prompt_tokens", "generated_tokens",
            "prompt_seconds", "decode_seconds", "total_seconds",
            "draft_accepted", "draft_generated", "draft_accept_rate", "draft_mean_len",
        )
        names = ", ".join(columns)
        marks = ", ".join("?" for _ in columns)
        updates = ", ".join(f"{col}=excluded.{col}" for col in columns[1:])
        with self.connect() as db:
            for row in rows:
                db.execute(f"INSERT INTO generations({names}) VALUES({marks}) "
                           f"ON CONFLICT(id) DO UPDATE SET {updates}",
                           [row.get(col) for col in columns])
            db.execute("INSERT INTO cursors(path, file_key, offset, run_id, gap) VALUES(?, ?, ?, ?, ?) "
                       "ON CONFLICT(path) DO UPDATE SET file_key=excluded.file_key, "
                       "offset=excluded.offset, run_id=excluded.run_id, gap=excluded.gap",
                       (path, file_key, offset, run_id, int(gap)))

    def latest_by_slot(self, run_id: str) -> dict[int, dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM generations WHERE run_id = ? AND slot_id IS NOT NULL "
                              "ORDER BY observed_at DESC, rowid DESC", (run_id,)).fetchall()
        result = {}
        for row in rows:
            slot = row["slot_id"]
            if slot not in result:
                result[slot] = dict(row)
        return result

    def latest_complete(self, run_id: Optional[str]) -> Optional[dict]:
        if not run_id:
            return None
        with self.connect() as db:
            row = db.execute("SELECT * FROM generations WHERE run_id = ? AND state = 'complete' "
                             "ORDER BY completed_at DESC, rowid DESC LIMIT 1", (run_id,)).fetchone()
            return dict(row) if row else None

    def list(self, *, limit: int = 50, cursor: Optional[str] = None,
             model: str = "", state: str = "", from_ts: Optional[float] = None,
             to_ts: Optional[float] = None, sort: str = "time",
             order: str = "desc") -> dict:
        if sort not in self.SORTS or order not in ("asc", "desc"):
            raise ValueError("unsupported history sort order")
        if state and state not in ("running", "complete", "incomplete", "ambiguous", "error", "prompt_only"):
            raise ValueError("unsupported history state")
        limit = max(1, min(200, limit))
        key = self.SORTS[sort]
        where, params = [], []
        if model:
            where.append("model = ?")
            params.append(model)
        if state:
            where.append("state = ?")
            params.append(state)
        if from_ts is not None:
            where.append("observed_at >= ?")
            params.append(from_ts)
        if to_ts is not None:
            where.append("observed_at < ?")
            params.append(to_ts)
        if cursor:
            try:
                value, row_id = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                if not isinstance(value, (int, float)) or not isinstance(row_id, str):
                    raise ValueError
            except Exception as exc:
                raise ValueError("invalid history cursor") from exc
            op = ">" if order == "asc" else "<"
            where.append(f"({key}, id) {op} (?, ?)")
            params.extend((value, row_id))
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self.connect() as db:
            rows = db.execute(f"WITH activity AS ("
                              "SELECT g.*, CASE WHEN p.prompt_text IS NOT NULL THEN 1 ELSE 0 END AS has_prompt, "
                              "COALESCE(p.truncated, 0) AS prompt_truncated FROM generations g "
                              "LEFT JOIN prompt_files p ON p.generation_id = g.id AND p.cleared = 0 "
                              "UNION ALL SELECT 'prompt:' || p.id AS id, p.run_id, NULL AS task_id, "
                              "NULL AS slot_id, r.model, p.captured_at AS observed_at, "
                              "NULL AS completed_at, 'prompt_only' AS state, NULL AS prompt_tokens, "
                              "NULL AS generated_tokens, NULL AS prompt_seconds, NULL AS decode_seconds, "
                              "NULL AS total_seconds, NULL AS draft_accepted, NULL AS draft_generated, "
                              "NULL AS draft_accept_rate, NULL AS draft_mean_len, 1 AS has_prompt, "
                              "p.truncated AS prompt_truncated FROM prompt_files p "
                              "JOIN runs r ON r.id = p.run_id "
                              "WHERE p.generation_id IS NULL AND p.cleared = 0) "
                              f"SELECT *, {key} AS sort_value FROM activity{clause} "
                              f"ORDER BY {key} {order.upper()}, id {order.upper()} LIMIT ?",
                              (*params, limit + 1)).fetchall()
            models = [r[0] for r in db.execute("SELECT DISTINCT model FROM ("
                "SELECT model FROM generations UNION SELECT model FROM runs WHERE id IN "
                "(SELECT run_id FROM prompt_files WHERE cleared = 0)) "
                "WHERE model IS NOT NULL ORDER BY model")]
        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = None
        if has_more and page:
            last = page[-1]
            next_cursor = base64.urlsafe_b64encode(
                json.dumps([last["sort_value"], last["id"]]).encode()).decode()
        return {"items": [{k: v for k, v in dict(r).items() if k != "sort_value"} for r in page],
                "next_cursor": next_cursor, "models": models}

    def clear(self, *, cleared_at: Optional[float] = None) -> int:
        with self.connect() as db:
            cleared_at = time.time() if cleared_at is None else cleared_at
            count = db.execute("SELECT COUNT(*) FROM generations").fetchone()[0]
            count += db.execute("SELECT COUNT(*) FROM prompt_files WHERE generation_id IS NULL AND cleared = 0").fetchone()[0]
            # Tombstones prevent native files still on disk from reappearing.
            db.execute("UPDATE prompt_files SET prompt_text = NULL, generation_id = NULL, cleared = 1")
            db.execute("DELETE FROM generations")
            # Keep cursors: clearing history must not replay old log content.
            db.execute("UPDATE runs SET prompts_cleared_at = ?", (cleared_at,))
            return count

    def get_prompt(self, activity_id: str) -> Optional[dict]:
        with self.connect() as db:
            if activity_id.startswith("prompt:"):
                row = db.execute("SELECT prompt_text, truncated FROM prompt_files "
                                 "WHERE id = ? AND generation_id IS NULL AND cleared = 0",
                                 (activity_id[7:],)).fetchone()
            else:
                row = db.execute("SELECT prompt_text, truncated FROM prompt_files "
                                 "WHERE generation_id = ? AND cleared = 0", (activity_id,)).fetchone()
            return dict(row) if row else None

    def has_prompt_file(self, run_id: str, path: str) -> bool:
        with self.connect() as db:
            return db.execute("SELECT 1 FROM prompt_files WHERE run_id = ? AND path = ?",
                              (run_id, path)).fetchone() is not None

    def managed_prompt_paths(self) -> list[str]:
        with self.connect() as db:
            return [row[0] for row in db.execute(
                "SELECT p.path FROM prompt_files p JOIN runs r ON r.id = p.run_id "
                "WHERE r.source = 'managed'")]

    def add_prompt_file(self, run_id: str, path: str, captured_at: float,
                        text: str, truncated: bool) -> None:
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO prompt_files "
                       "(id, run_id, path, captured_at, prompt_text, truncated) "
                       "VALUES (?, ?, ?, ?, ?, ?)",
                       (uuid.uuid4().hex, run_id, path, captured_at, text, int(truncated)))

    def match_prompts(self, run_id: str, *, now: Optional[float] = None) -> None:
        """Link only uniquely paired prompts and generations in a short time window."""
        now = time.time() if now is None else now
        with self.connect() as db:
            prompts = db.execute("SELECT id, captured_at FROM prompt_files WHERE run_id = ? "
                                 "AND generation_id IS NULL AND cleared = 0", (run_id,)).fetchall()
            if not prompts:
                return
            low = min(p["captured_at"] for p in prompts) - 2
            high = max(p["captured_at"] for p in prompts) + 10
            generations = db.execute("SELECT id, observed_at FROM generations WHERE run_id = ? "
                                     "AND observed_at BETWEEN ? AND ? "
                                     "AND id NOT IN (SELECT generation_id FROM prompt_files "
                                     "WHERE generation_id IS NOT NULL) AND slot_id IS NOT NULL",
                                     (run_id, low, high)).fetchall()
            for prompt in prompts:
                candidates = [g for g in generations if -2 <= g["observed_at"] - prompt["captured_at"] <= 10]
                if len(candidates) != 1:
                    continue
                generation = candidates[0]
                # Wait for other native files from the same burst to finish.
                # Otherwise the first file scanned could be matched prematurely.
                if now - generation["observed_at"] < 5:
                    continue
                competitors = [p for p in prompts if -2 <= generation["observed_at"] - p["captured_at"] <= 10]
                if len(competitors) == 1:
                    db.execute("UPDATE prompt_files SET generation_id = ? WHERE id = ?",
                               (generation["id"], prompt["id"]))


class LogFollower:
    """Single log reader for history, idle gating, and startup metadata."""

    _SLOT = re.compile(r"\bid\s+(\d+)\b", re.I)
    _TASK = re.compile(r"\btask\s+(\d+)\b", re.I)
    _ERROR = re.compile(r"\berror\b", re.I)
    MAX_READ = 4 * 1024 * 1024
    MAX_PROMPT = 1024 * 1024

    def __init__(self, db: HistoryDB, path: Optional[str] = None,
                 run_id: Optional[str] = None, managed: bool = False,
                 started_at: Optional[float] = None,
                 prompt_dir: Optional[str] = None):
        self.db = db
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.path: Optional[str] = None
        self.prompt_dir: Optional[str] = None
        self._prompt_pending: dict[str, tuple[int, float]] = {}
        self.run_id: Optional[str] = None
        self.started_at: Optional[float] = None
        self.model: Optional[str] = None
        self._model_dirty = False
        self.managed = managed
        self.file_key: Optional[str] = None
        self.offset = 0
        self.available = False
        self.gap = False
        self.error: Optional[str] = None
        self.activity_seq = 0
        self.slots: dict[int, dict] = {}
        self.split: dict[str, float] = {}
        self.baseline: dict[str, float] = {}
        self.spec = False
        self.attach(path, run_id=run_id, managed=managed, started_at=started_at,
                    prompt_dir=prompt_dir)

    @staticmethod
    def _key(stat) -> str:
        return f"{stat.st_dev}:{stat.st_ino}"

    def attach(self, path: Optional[str], *, run_id: Optional[str] = None,
               managed: bool = False, started_at: Optional[float] = None,
               prompt_dir: Optional[str] = None) -> None:
        with self.lock:
            self.path = os.path.abspath(path) if path else None
            self.prompt_dir = os.path.abspath(prompt_dir) if prompt_dir else None
            self._prompt_pending = {}
            self.run_id = run_id
            self.started_at = started_at
            self.model = None
            self._model_dirty = False
            self.managed = managed
            self.file_key = None
            self.offset = 0
            self.available = False
            self.gap = False
            self.error = None
            self.slots = {}
            self.split = {}
            self.baseline = {}
            self.spec = False
            if not self.path:
                return
            previous = self.db.get_cursor(self.path)
            if previous:
                self.gap = bool(previous["gap"])
            try:
                stat = os.stat(self.path)
                self.file_key = self._key(stat)
                self.available = True
            except OSError:
                stat = None
            if run_id:
                self.run_id = run_id
                if previous and previous["run_id"] == run_id and stat and \
                        previous["file_key"] == self.file_key and previous["offset"] <= stat.st_size:
                    self.offset = previous["offset"]
                elif previous and previous["run_id"] == run_id and stat:
                    self.gap = True
            elif previous and stat and previous["file_key"] == self.file_key and \
                    previous["offset"] <= stat.st_size:
                self.run_id = previous["run_id"]
                self.offset = previous["offset"]
            else:
                if previous and stat:
                    self.gap = True
                self.run_id = uuid.uuid4().hex
                # A newly watched external log begins at its current end.
                self.offset = 0 if managed else (stat.st_size if stat else 0)
            self.started_at = started_at or self.db.run_started_at(self.run_id) or time.time()
            self.db.ensure_run(self.run_id, "managed" if managed else "external",
                               self.started_at)
            if stat:
                self.db.save_batch(self.path, self.file_key, self.offset, self.run_id, [], self.gap)
                if self.offset:
                    self._scan_metadata()
            self.slots = self.db.latest_by_slot(self.run_id)

    def set_model(self, model: Optional[str]) -> None:
        if not model or model == "unknown":
            return
        with self.lock:
            if self.run_id and model != self.model:
                self.model = model
                self._model_dirty = True
                for row in self.slots.values():
                    row["model"] = model

    def _scan_metadata(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path, "rb") as f:
                while f.tell() < self.offset:
                    raw = f.readline(self.offset - f.tell())
                    if not raw.endswith(b"\n"):
                        break
                    msg = decode_log_line(raw.decode("utf-8", "replace"))
                    if msg:
                        self._metadata(msg)
        except OSError:
            pass

    def _metadata(self, msg: str) -> None:
        if msg.lower().startswith("build:"):
            self.split = {}
            self.baseline = {}
            self.spec = False
        for label, mib in _BUF_RE.findall(msg):
            name = _norm_device(label)
            self.split[name] = self.split.get(name, 0.0) + float(mib)
        for label, free in _DEVINFO_RE.findall(msg):
            self.baseline[label.upper()] = float(free)
        self.spec |= bool(_SPEC_RE.search(msg))

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name="llama-log-follower", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3)

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.poll()
            except Exception as exc:
                with self.lock:
                    self.error = str(exc)
                    self.available = False
            self.stop_event.wait(1.0)

    def snapshot(self) -> dict:
        with self.lock:
            return {"available": self.available, "configured": bool(self.path),
                    "gap": self.gap, "error": self.error, "run_id": self.run_id,
                    "prompts_configured": bool(self.prompt_dir),
                    "activity_seq": self.activity_seq, "split": dict(self.split),
                    "baseline": dict(self.baseline), "spec": self.spec}

    def _poll_prompts(self) -> None:
        if not self.prompt_dir or not self.run_id:
            return
        cutoff = max(self.started_at or 0, self.db.prompt_clear_at(self.run_id) or 0)
        try:
            entries = list(os.scandir(self.prompt_dir))
        except FileNotFoundError:
            return
        for entry in entries:
            if not entry.name.endswith(".txt") or not entry.name[:-4].isdigit():
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                stat = entry.stat(follow_symlinks=False)
                if stat.st_mtime <= cutoff:
                    continue
                if time.time() - stat.st_mtime < 0.5:
                    continue  # the server may still be writing this file
                if self.db.has_prompt_file(self.run_id, entry.path):
                    continue
                signature = (stat.st_size, stat.st_mtime)
                if self._prompt_pending.get(entry.path) != signature:
                    self._prompt_pending[entry.path] = signature
                    continue
                with open(entry.path, "rb") as f:
                    raw = f.read(self.MAX_PROMPT + 1)
                after = entry.stat(follow_symlinks=False)
                if (after.st_size, after.st_mtime) != signature:
                    self._prompt_pending[entry.path] = (after.st_size, after.st_mtime)
                    continue
                self.db.add_prompt_file(self.run_id, entry.path, stat.st_mtime,
                                        raw[:self.MAX_PROMPT].decode("utf-8", "replace"),
                                        len(raw) > self.MAX_PROMPT)
                self._prompt_pending.pop(entry.path, None)
            except FileNotFoundError:
                continue  # file disappeared between scan and read
        self.db.match_prompts(self.run_id)

    def clear_history(self) -> int:
        with self.lock:
            cleared_at = time.time()
            managed_files = self.db.managed_prompt_paths()
            count = self.db.clear(cleared_at=cleared_at)
            self.slots = {}
            self._prompt_pending = {}
            # Clearing history also removes files written by launches we manage.
            # External server files belong to their owner and are left alone.
            roots = [os.path.realpath(store.PROMPTS_DIR)]
            if self.managed and self.prompt_dir:
                roots.append(os.path.realpath(self.prompt_dir))
            for path in managed_files:
                try:
                    resolved = os.path.realpath(path)
                    if any(os.path.commonpath((resolved, root)) == root for root in roots):
                        os.unlink(path)
                except (OSError, ValueError):
                    pass
            # Include files the follower had not imported yet. The timestamp
            # boundary above prevents a failed deletion from replaying them.
            try:
                for entry in os.scandir(store.PROMPTS_DIR):
                    if entry.name.endswith(".txt") and entry.name[:-4].isdigit() and \
                            entry.is_file(follow_symlinks=False) and \
                            entry.stat(follow_symlinks=False).st_mtime <= cleared_at:
                        try:
                            os.unlink(entry.path)
                        except OSError:
                            pass
            except FileNotFoundError:
                pass
            return count

    def _close_running(self) -> None:
        closing = []
        for row in self.slots.values():
            if row["state"] == "running":
                closing.append({**row, "state": "incomplete"})
        if closing and self.path and self.file_key and self.run_id:
            self.db.save_batch(self.path, self.file_key, self.offset,
                               self.run_id, closing, self.gap)
            for row in self.slots.values():
                if row["state"] == "running":
                    row["state"] = "incomplete"

    def finish_segment(self) -> None:
        """Drain a stopped server's final lines before its log is reset."""
        with self.lock:
            try:
                self.poll()
                self._close_running()
            except Exception as exc:
                self.error = str(exc)
                self.gap = True

    def poll(self) -> None:
        with self.lock:
            if self._model_dirty and self.run_id and self.model:
                self.db.set_model(self.run_id, self.model)
                self._model_dirty = False
            if not self.path or not self.run_id:
                return
            self._poll_prompts()
            try:
                stat = os.stat(self.path)
            except OSError as exc:
                if self.available:
                    self.gap = True
                    self._close_running()
                    if self.file_key:
                        self.db.save_batch(self.path, self.file_key, self.offset,
                                           self.run_id, [], self.gap)
                self.available = False
                self.error = str(exc)
                return
            self.available = True
            self.error = None
            key = self._key(stat)
            new_segment = False
            if self.file_key is None:
                self.file_key = key
                if not self.managed:
                    self.offset = stat.st_size
                    self._scan_metadata()
                self.db.save_batch(self.path, key, self.offset, self.run_id, [], self.gap)
            elif self.file_key != key or stat.st_size < self.offset:
                self.gap = True
                new_segment = True
                self._close_running()
                self.file_key = key
                self.offset = 0
                if not self.managed:
                    self.run_id = uuid.uuid4().hex
                self.started_at = time.time()
                if not self.managed:
                    self.model = None
                    self._model_dirty = False
                self.slots = {}
                self.split = {}
                self.baseline = {}
                self.spec = False
                self.db.ensure_run(self.run_id, "managed" if self.managed else "external",
                                   self.started_at)
            if stat.st_size == self.offset:
                if new_segment:
                    self.db.save_batch(self.path, self.file_key, self.offset,
                                       self.run_id, [], self.gap)
                return
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                data = f.read(self.MAX_READ)
            end = data.rfind(b"\n")
            if end < 0:
                if len(data) == self.MAX_READ:
                    # Bound memory use even if a malformed log emits a huge line.
                    self.offset += len(data)
                    self.gap = True
                    self.db.save_batch(self.path, self.file_key, self.offset,
                                       self.run_id, [], self.gap)
                return
            complete = data[:end + 1]
            rows: dict[str, dict] = {}
            before = (copy.deepcopy(self.slots), dict(self.split), dict(self.baseline),
                      self.spec, self.activity_seq, self.run_id, self.model,
                      self.started_at, self._model_dirty)
            try:
                for raw in complete.splitlines():
                    try:
                        obj = json.loads(raw) if raw.lstrip().startswith(b"{") else None
                    except (ValueError, TypeError):
                        obj = None
                    msg = decode_log_line(raw.decode("utf-8", "replace"))
                    if not msg:
                        continue
                    if not self.managed and msg.lower().startswith("build:"):
                        for old in self.slots.values():
                            if old["state"] == "running":
                                rows[old["id"]] = {**old, "state": "incomplete"}
                        self.run_id = uuid.uuid4().hex
                        self.started_at = time.time()
                        self.model = None
                        self._model_dirty = False
                        self.slots = {}
                        self.db.ensure_run(self.run_id, "external", self.started_at)
                    self._metadata(msg)
                    if LogTailer._ACT.search(msg) or (not LogTailer._IDLE.search(msg)
                            and not LogTailer._NOISE.search(msg)):
                        self.activity_seq += 1
                    observed = time.time()
                    if isinstance(obj, dict) and isinstance(obj.get("time"), (int, float)) \
                            and obj["time"] > 0 and self.managed and self.started_at:
                        observed = self.started_at + obj["time"] / 1_000_000
                    for row in self._generation(msg, observed):
                        rows[row["id"]] = row.copy()
                # Rows and cursor advance in one transaction. On write failure,
                # restore the in-memory parser and retry from the old offset.
                self.db.save_batch(self.path, self.file_key, self.offset + len(complete),
                                   self.run_id, list(rows.values()), self.gap)
                self.offset += len(complete)
                if self.prompt_dir:
                    self.db.match_prompts(self.run_id)
            except Exception:
                (self.slots, self.split, self.baseline, self.spec, self.activity_seq,
                 self.run_id, self.model, self.started_at, self._model_dirty) = before
                raise

    def _generation(self, msg: str, observed: float) -> list[dict]:
        match = self._SLOT.search(msg)
        if not match:
            # Without a slot, concurrent timings cannot be safely correlated.
            # Keep each timing line as its own visible ambiguous observation.
            timing = {}
            for pattern, tokens, seconds in (
                (LogTailer._PROMPT_EVAL, "prompt_tokens", "prompt_seconds"),
                (LogTailer._DECODE_EVAL, "generated_tokens", "decode_seconds"),
                (LogTailer._TOTAL_EVAL, None, "total_seconds"),
            ):
                found = pattern.search(msg)
                if found:
                    timing[seconds] = float(found.group(1)) / 1000
                    if tokens:
                        timing[tokens] = int(found.group(2))
            if not timing:
                return []
            return [{"id": uuid.uuid4().hex, "run_id": self.run_id,
                     "task_id": None, "slot_id": None, "model": self.model,
                     "observed_at": observed, "completed_at": None,
                     "state": "ambiguous", **timing}]
        slot = int(match.group(1))
        task_match = self._TASK.search(msg)
        task = int(task_match.group(1)) if task_match else None
        start = "processing task" in msg.lower()
        has_timing = any(pattern.search(msg) for pattern in (
            LogTailer._PROMPT_EVAL, LogTailer._DECODE_EVAL, LogTailer._TOTAL_EVAL,
            LogTailer._DRAFT, LogTailer._ACCLEN))
        stopped = "stop processing" in msg.lower()
        failed = bool(self._ERROR.search(msg))
        if not (start or has_timing or stopped or failed):
            return []
        row = self.slots.get(slot)
        changed_rows = []
        if row and row["state"] in ("complete", "incomplete", "error") and \
                (LogTailer._PROMPT_EVAL.search(msg) or LogTailer._DECODE_EVAL.search(msg)):
            row = None
        if row and not start and task is not None and row.get("task_id") is not None \
                and task != row["task_id"]:
            if row["state"] == "running":
                row["state"] = "incomplete"
                changed_rows.append(row.copy())
            row = None
        if start or row is None:
            if row and row["state"] == "running":
                row["state"] = "incomplete"
                changed_rows.append(row.copy())
            row = {"id": uuid.uuid4().hex, "run_id": self.run_id,
                   "task_id": task, "slot_id": slot, "model": self.model,
                   "observed_at": observed, "completed_at": None,
                   "state": "running" if start else "incomplete"}
            self.slots[slot] = row
        elif task is not None:
            row["task_id"] = task
        changed = start
        for name, pattern, tokens_field, seconds_field in (
            ("prompt", LogTailer._PROMPT_EVAL, "prompt_tokens", "prompt_seconds"),
            ("decode", LogTailer._DECODE_EVAL, "generated_tokens", "decode_seconds"),
        ):
            m = pattern.search(msg)
            if m:
                row[tokens_field] = int(m.group(2))
                row[seconds_field] = float(m.group(1)) / 1000
                changed = True
        m = LogTailer._TOTAL_EVAL.search(msg)
        if m:
            row["total_seconds"] = float(m.group(1)) / 1000
            row["completed_at"] = observed
            row["state"] = "complete" if row.get("prompt_seconds") is not None \
                and row.get("decode_seconds") is not None else "incomplete"
            changed = True
        m = LogTailer._DRAFT.search(msg)
        if m:
            row["draft_accept_rate"] = float(m.group(1))
            row["draft_accepted"] = int(m.group(2))
            row["draft_generated"] = int(m.group(3))
            changed = True
        m = LogTailer._ACCLEN.search(msg)
        if m:
            row["draft_mean_len"] = float(m.group(1))
            changed = True
        if "stop processing" in msg.lower() and row["state"] == "running":
            row["state"] = "incomplete"
            changed = True
        if self._ERROR.search(msg) and row["state"] != "complete":
            row["state"] = "error"
            changed = True
        if changed:
            changed_rows.append(row)
        return changed_rows
