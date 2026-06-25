"""Discover the flags supported by the installed llama-server.

The launch panel offers a dropdown of flags and shows a short description next to
each one. Rather than hardcode a list that drifts from the user's build, we run
``llama-server --help`` once, parse it, and serve the result. The output looks
like::

    ----- common params -----

    -h,    --help, --usage          print usage and exit
    -c,    --ctx-size N             size of the prompt context (default: 4096)
                                    (env: LLAMA_ARG_CTX_SIZE)
        --mmap                      enable memory-mapping (default: enabled)

Each entry is reshaped into ``{"flags": [...aliases...], "value_hint": "N"|None,
"desc": "..."}``. Parsing is deliberately tolerant: unrecognised lines are
skipped and nothing here raises, so a help-format change at worst yields a
shorter list (and we fall back to :data:`BUNDLED_FLAGS`).
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Optional

# A run of two-or-more spaces separates the flag column from the description.
_GAP = re.compile(r"\s{2,}")
# Section banners like "----- common params -----".
_SECTION = re.compile(r"^\s*-{3,}.*-{3,}\s*$")
# The "(env: LLAMA_ARG_*)" continuation we don't want in the description.
_ENV = re.compile(r"\(env:\s*[^)]*\)")
_HELP_TIMEOUT = 8.0


# One alias (``-c`` / ``--ctx-size``) optionally followed by its comma separator.
# Aliases are ", "-joined; the value hint follows the last alias after a space, so
# the hint may itself contain commas/spaces (e.g. ``{none,layer,row}``).
_ALIAS = re.compile(r"\s*(-{1,2}[^\s,]+)\s*(,)?")


def _split_flag_column(col: str) -> tuple[list[str], Optional[str]]:
    """Split e.g. ``-c, --ctx-size N`` into (["-c", "--ctx-size"], "N").

    Walks the leading aliases (each starting with ``-``, comma-separated); once an
    alias is not followed by a comma, the remainder is the value hint — so hints
    containing commas (``--split-mode {none,layer,row}``) stay intact.
    """
    col = col.strip()
    flags: list[str] = []
    hint: Optional[str] = None
    pos = 0
    while pos < len(col):
        m = _ALIAS.match(col, pos)
        if not m:
            break
        flags.append(m.group(1))
        pos = m.end()
        if not m.group(2):                 # no trailing comma -> rest is the hint
            rest = col[pos:].strip()
            hint = rest or None
            break
    return flags, hint


def parse_help(text: str) -> list[dict]:
    """Parse ``llama-server --help`` text into flag descriptors."""
    entries: list[dict] = []
    current: Optional[dict] = None

    def flush() -> None:
        nonlocal current
        if current and current["flags"]:
            current["desc"] = _ENV.sub("", " ".join(current["desc"]).strip())
            current["desc"] = re.sub(r"\s{2,}", " ", current["desc"]).strip()
            entries.append({k: current[k] for k in ("flags", "value_hint", "desc")})
        current = None

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or _SECTION.match(line):
            continue
        # A real flag entry begins at column 0; description text, wrapped lines
        # and enumerated sub-values ("- none: ...") are indented continuations —
        # even when they start with a dash.
        indent = len(line) - len(line.lstrip())
        stripped = line.lstrip()
        if indent == 0 and stripped.startswith("-"):
            # New flag entry. llama-server pads the first alias to a fixed width
            # (e.g. "-h,    --help"), so there can be a 2+ space gap *inside* the
            # flag column. The description (when on the same line) is after the
            # LAST big gap; a gap whose tail still looks like a flag is just that
            # alias padding, so the description is on the following line(s).
            flush()
            gaps = list(_GAP.finditer(stripped))
            col, desc = stripped, ""
            if gaps:
                last = gaps[-1]
                tail = stripped[last.end():].strip()
                if tail and not tail.startswith("-"):
                    col = stripped[:last.start()].rstrip()
                    desc = tail
            flag_list, hint = _split_flag_column(col)
            current = {"flags": flag_list, "value_hint": hint, "desc": [desc] if desc else []}
        elif current is not None:
            # Indented continuation line -> part of the current description.
            current["desc"].append(stripped)
    flush()
    return entries


# Static fallback used when the binary can't be run (covers the common flags so
# the panel still works with no llama-server installed).
BUNDLED_FLAGS: list[dict] = [
    {"flags": ["-a", "--alias"], "value_hint": "STRING", "desc": "model alias shown in the API"},
    {"flags": ["-b", "--batch-size"], "value_hint": "N", "desc": "logical maximum batch size"},
    {"flags": ["-c", "--ctx-size"], "value_hint": "N", "desc": "size of the prompt context (0 = loaded from model)"},
    {"flags": ["--cache-type-k"], "value_hint": "TYPE", "desc": "KV cache data type for K"},
    {"flags": ["--cache-type-v"], "value_hint": "TYPE", "desc": "KV cache data type for V"},
    {"flags": ["-fa", "--flash-attn"], "value_hint": "on/off/auto", "desc": "set Flash Attention use"},
    {"flags": ["--host"], "value_hint": "HOST", "desc": "ip address to listen on"},
    {"flags": ["-md", "--model-draft"], "value_hint": "FNAME", "desc": "draft model for speculative decoding"},
    {"flags": ["--mlock"], "value_hint": None, "desc": "force system to keep model in RAM"},
    {"flags": ["-ngl", "--gpu-layers"], "value_hint": "N", "desc": "number of layers to store in VRAM"},
    {"flags": ["--no-mmap"], "value_hint": None, "desc": "do not memory-map model (slower load but may reduce pageouts)"},
    {"flags": ["-np", "--parallel"], "value_hint": "N", "desc": "number of parallel sequences to decode"},
    {"flags": ["--rope-scaling"], "value_hint": "TYPE", "desc": "RoPE frequency scaling method"},
    {"flags": ["--spec-type"], "value_hint": "TYPE", "desc": "speculative decoding type (e.g. draft-mtp)"},
    {"flags": ["-t", "--threads"], "value_hint": "N", "desc": "number of CPU threads to use during generation"},
    {"flags": ["-ts", "--tensor-split"], "value_hint": "SPLIT", "desc": "fraction of the model to offload to each GPU"},
    {"flags": ["-ub", "--ubatch-size"], "value_hint": "N", "desc": "physical maximum batch size"},
]

# Cache the parsed help keyed by (binary path, mtime) so we run --help at most
# once per build rather than on every panel refresh.
_cache: dict[tuple, list[dict]] = {}


def _binary_key(binary: str) -> tuple:
    try:
        return (binary, os.path.getmtime(binary))
    except Exception:
        return (binary, None)


def get_server_flags(binary: Optional[str]) -> dict:
    """Return ``{"flags": [...], "source": "help"|"bundled"}`` for *binary*.

    Runs ``binary --help`` (cached per build) and parses it; on any failure
    (no binary, timeout, empty parse) falls back to :data:`BUNDLED_FLAGS`.
    """
    if not binary:
        return {"flags": BUNDLED_FLAGS, "source": "bundled"}

    key = _binary_key(binary)
    if key in _cache:
        return {"flags": _cache[key], "source": "help"}

    try:
        proc = subprocess.run(
            [binary, "--help"],
            capture_output=True,
            text=True,
            timeout=_HELP_TIMEOUT,
        )
        text = (proc.stdout or "") + "\n" + (proc.stderr or "")
        parsed = parse_help(text)
    except Exception:
        parsed = []

    if not parsed:
        return {"flags": BUNDLED_FLAGS, "source": "bundled"}

    _cache[key] = parsed
    return {"flags": parsed, "source": "help"}
