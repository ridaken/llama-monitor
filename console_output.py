"""Presentation metadata for console readers; never used by log capture."""
import json


def entry(raw: str, text: str) -> dict:
    """Keep the existing displayed text and expose supported record metadata."""
    result = {"text": text}
    original = raw
    try:
        record = json.loads(raw)
    except ValueError:
        record = None
    if isinstance(record, dict):
        for key in ("type", "level"):
            if isinstance(record.get(key), str):
                result[key] = record[key]
        if isinstance(record.get("msg"), str):
            original = record["msg"]
    # Legacy decoding strips indentation. Preserve this hint so stack frames
    # can follow their diagnostic even when they arrive in a later poll.
    if original[:1].isspace() or original.startswith(("File ", "at ")):
        result["continuation"] = True
    return result
