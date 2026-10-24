"""Proxy request records, deliberately independent of log-derived generations."""
from __future__ import annotations

import base64
import json
import math
import time


class RequestHistory:
    def __init__(self, history):
        self.history = history
        with history.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS proxy_requests (
                    id TEXT PRIMARY KEY, observed_at REAL NOT NULL,
                    model TEXT, endpoint TEXT NOT NULL, state TEXT NOT NULL,
                    metadata TEXT NOT NULL, request_text TEXT, response_text TEXT
                );
                CREATE INDEX IF NOT EXISTS proxy_requests_time ON proxy_requests(observed_at, id);
                CREATE INDEX IF NOT EXISTS proxy_requests_model ON proxy_requests(model, observed_at);
                CREATE TABLE IF NOT EXISTS proxy_capture_control (
                    id INTEGER PRIMARY KEY CHECK(id = 1), cleared_at REAL NOT NULL
                );
                INSERT OR IGNORE INTO proxy_capture_control(id, cleared_at) VALUES(1, 0);
            """)
            for row in db.execute("SELECT id, metadata FROM proxy_requests WHERE state = 'running'").fetchall():
                metadata = json.loads(row["metadata"])
                metadata.update(state="interrupted", completed_at=time.time())
                db.execute("UPDATE proxy_requests SET state='interrupted', metadata=? WHERE id=?",
                           (json.dumps(metadata), row["id"]))

    def begin(self, metadata):
        with self.history.connect() as db:
            db.execute("INSERT INTO proxy_requests(id, observed_at, endpoint, state, metadata) "
                       "SELECT ?,?,?,?,? WHERE ? > (SELECT cleared_at FROM proxy_capture_control WHERE id=1)",
                       (metadata["id"], metadata["observed_at"], metadata["endpoint"], "running",
                        json.dumps(metadata), metadata["observed_at"]))

    def finish(self, metadata, request_text, response_text):
        # UPDATE only: a clear during inference must never resurrect a record.
        with self.history.connect() as db:
            db.execute("UPDATE proxy_requests SET model=?, state=?, metadata=?, request_text=?, response_text=? WHERE id=?",
                       (metadata.get("model"), metadata["state"], json.dumps(metadata),
                        request_text, response_text, metadata["id"]))

    def clear(self):
        with self.history.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM proxy_requests").fetchone()[0]
            db.execute("UPDATE proxy_capture_control SET cleared_at=? WHERE id=1", (time.time(),))
            db.execute("DELETE FROM proxy_requests")
            return count

    def get(self, request_id):
        with self.history.connect() as db:
            row = db.execute("SELECT metadata, request_text, response_text FROM proxy_requests WHERE id=?",
                             (request_id,)).fetchone()
        return {**json.loads(row["metadata"]), "request_text": row["request_text"],
                "response_text": row["response_text"]} if row else None

    def list(self, *, model="", endpoint="", state="", from_ts=None, to_ts=None, cursor=None, limit=50):
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        clauses, params = [], []
        for key, value in (("model", model), ("endpoint", endpoint), ("state", state)):
            if value:
                clauses.append(f"{key} = ?")
                params.append(value)
        for key, value, op in (("from_ts", from_ts, ">="), ("to_ts", to_ts, "<")):
            if value is not None:
                if not math.isfinite(value):
                    raise ValueError(f"{key} must be finite")
                clauses.append(f"observed_at {op} ?")
                params.append(value)
        if cursor:
            try:
                stamp, identifier = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                if not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or not isinstance(identifier, str):
                    raise ValueError
            except Exception as exc:
                raise ValueError("invalid request cursor") from exc
            clauses.append("(observed_at, id) < (?, ?)")
            params.extend((stamp, identifier))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.history.connect() as db:
            rows = db.execute("SELECT metadata FROM proxy_requests" + where +
                              " ORDER BY observed_at DESC, id DESC LIMIT ?", (*params, limit + 1)).fetchall()
        items = [json.loads(row["metadata"]) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = items[-1]
            next_cursor = base64.urlsafe_b64encode(json.dumps([last["observed_at"], last["id"]]).encode()).decode()
        return {"items": items, "next_cursor": next_cursor}
