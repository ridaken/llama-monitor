"""A read-only, source-labelled timeline over the two existing history stores."""
from __future__ import annotations

import base64
import hashlib
import json
import math


class ActivityHistory:
    SORTS = {"time": "observed_at", "duration": "total_seconds",
             "prompt_tokens": "prompt_tokens", "generated_tokens": "generated_tokens"}
    STATES = {"running", "complete", "incomplete", "ambiguous", "error", "prompt_only",
              "cancelled", "interrupted"}

    def __init__(self, history):
        self.history = history

    def list(self, *, source="all", model="", endpoint="", state="", from_ts=None,
             to_ts=None, sort="time", order="desc", cursor=None, limit=50, include_proxy=True):
        if source not in ("all", "proxy", "monitored"):
            raise ValueError("unsupported activity source")
        if sort not in self.SORTS or order not in ("asc", "desc"):
            raise ValueError("unsupported activity sort order")
        if state and state not in self.STATES:
            raise ValueError("unsupported activity state")
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        for value in (from_ts, to_ts):
            if value is not None and not math.isfinite(value):
                raise ValueError("date bounds must be finite")
        signature = hashlib.sha256(json.dumps(
            [source, model, endpoint, state, from_ts, to_ts, sort, order],
            separators=(",", ":")).encode()).hexdigest()
        clauses, params = [], []
        for field, value in (("source", source if source != "all" else ""),
                             ("model", model), ("endpoint", endpoint), ("state", state)):
            if value:
                clauses.append(f"{field} = ?")
                params.append(value)
        for value, op in ((from_ts, ">="), (to_ts, "<")):
            if value is not None:
                clauses.append(f"observed_at {op} ?")
                params.append(value)
        if cursor:
            try:
                version, bound, missing, value, origin, identifier = json.loads(
                    base64.b64decode(cursor.encode(), altchars=b"-_", validate=True))
                if version != 1 or bound != signature or type(missing) is not int or missing not in (0, 1) \
                        or type(value) not in (int, float) or not math.isfinite(value) \
                        or origin not in ("monitored", "proxy") or not isinstance(identifier, str):
                    raise ValueError
            except Exception as exc:
                raise ValueError("invalid or incompatible activity cursor") from exc
            op = ">" if order == "asc" else "<"
            clauses.append(f"(missing > ? OR (missing = ? AND "
                           f"(COALESCE(sort_value, 0), source, record_id) {op} (?, ?, ?)))")
            params.extend((missing, missing, value, origin, identifier))
        # Metadata contains no payload text. Native prompt-only rows follow the
        # same rules as HistoryDB.list; neither source is correlated or suppressed.
        monitored = """
            SELECT 'monitored' AS source, g.id AS record_id, g.observed_at, g.model,
                g.state, NULL AS endpoint, g.total_seconds, g.prompt_tokens, g.generated_tokens,
                json_object('run_id', g.run_id, 'slot_id', g.slot_id, 'task_id', g.task_id,
                    'has_prompt', CASE WHEN p.prompt_text IS NOT NULL THEN 1 ELSE 0 END,
                    'prompt_truncated', COALESCE(p.truncated, 0),
                    'prompt_seconds', g.prompt_seconds, 'decode_seconds', g.decode_seconds,
                    'draft_accepted', g.draft_accepted, 'draft_generated', g.draft_generated,
                    'draft_accept_rate', g.draft_accept_rate, 'draft_mean_len', g.draft_mean_len) AS metadata
            FROM generations g LEFT JOIN prompt_files p ON p.generation_id = g.id AND p.cleared = 0
            UNION ALL
            SELECT 'monitored', 'prompt:' || p.id, p.captured_at, r.model, 'prompt_only',
                NULL, NULL, NULL, NULL,
                json_object('run_id', p.run_id, 'has_prompt', 1, 'prompt_truncated', p.truncated)
            FROM prompt_files p JOIN runs r ON r.id = p.run_id
            WHERE p.generation_id IS NULL AND p.cleared = 0
        """
        proxy = """
            UNION ALL
            SELECT 'proxy', id, observed_at, COALESCE(model, json_extract(metadata, '$.model')), state, endpoint,
                CASE WHEN json_type(metadata, '$.duration_seconds') IN ('integer', 'real')
                    THEN json_extract(metadata, '$.duration_seconds') END,
                CASE WHEN json_type(metadata, '$.usage.prompt_tokens') IN ('integer', 'real')
                    THEN json_extract(metadata, '$.usage.prompt_tokens') END,
                CASE WHEN json_type(metadata, '$.usage.completion_tokens') IN ('integer', 'real')
                    THEN json_extract(metadata, '$.usage.completion_tokens') END,
                metadata FROM proxy_requests
        """ if include_proxy else ""
        cte = f"WITH activity AS ({monitored}{proxy})"
        key = self.SORTS[sort]
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        direction = order.upper()
        with self.history.connect() as db:
            rows = db.execute(cte + f", ordered AS (SELECT *, {key} AS sort_value, "
                              f"({key} IS NULL) AS missing FROM activity) SELECT * FROM ordered{where} "
                              f"ORDER BY missing ASC, COALESCE(sort_value, 0) {direction}, "
                              f"source {direction}, record_id {direction} LIMIT ?",
                              (*params, limit + 1)).fetchall()
            models = [row[0] for row in db.execute(cte +
                      " SELECT DISTINCT model FROM activity WHERE model IS NOT NULL ORDER BY model")]
        items = [{**json.loads(row["metadata"]),
                  **{key: row[key] for key in ("source", "record_id", "observed_at", "model", "state",
                                              "endpoint", "total_seconds", "prompt_tokens", "generated_tokens")},
                  "id": f'{row["source"]}:{row["record_id"]}'} for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = base64.urlsafe_b64encode(json.dumps(
                [1, signature, last["missing"], last["sort_value"] or 0,
                 last["source"], last["record_id"]]).encode()).decode()
        return {"items": items, "models": models, "next_cursor": next_cursor}
