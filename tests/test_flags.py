"""Tests for flags.py — parsing `llama-server --help` into flag descriptors and
falling back to the bundled list. The parser is deliberately tolerant, so these
lock down the structure it must keep producing."""

import flags
from flags import BUNDLED_FLAGS, get_server_flags, parse_help


# A representative slice of real `llama-server --help` output: a section banner,
# short+long aliases (first alias padded to a fixed width, so there's a 2+ space
# gap *inside* the flag column), a value hint, a wrapped description with an
# (env:) continuation, a comma-containing hint, and a bare boolean switch. Flag
# entries start at column 0; continuations are indented.
SAMPLE = """\
----- common params -----

-h,    --help, --usage          print usage and exit
-c,    --ctx-size N             size of the prompt context (default: 4096, 0 =
                                loaded from model)
                                (env: LLAMA_ARG_CTX_SIZE)
--mlock                         force system to keep model in RAM rather than
                                swapping or compressing
-sm,   --split-mode {none,layer,row}  how to split the model across GPUs
-np,   --parallel N             number of parallel sequences to decode (default: 1)
"""


def _by_flag(entries):
    return {tuple(e["flags"]): e for e in entries}


def test_parse_help_extracts_aliases_hint_and_desc():
    entries = parse_help(SAMPLE)
    idx = _by_flag(entries)

    assert ("-h", "--help", "--usage") in idx
    ctx = idx[("-c", "--ctx-size")]
    assert ctx["value_hint"] == "N"
    # Wrapped continuation joined; the (env:) line dropped.
    assert ctx["desc"] == "size of the prompt context (default: 4096, 0 = loaded from model)"
    assert "env:" not in ctx["desc"]


def test_parse_help_handles_bare_switch_and_section_headers():
    entries = parse_help(SAMPLE)
    idx = _by_flag(entries)
    # Section banner produced no entry.
    assert all("params" not in "".join(e["flags"]) for e in entries)
    mlock = idx[("--mlock",)]
    assert mlock["value_hint"] is None
    assert mlock["desc"].startswith("force system to keep model in RAM")
    # A value hint that itself contains commas must stay intact.
    assert idx[("-sm", "--split-mode")]["value_hint"] == "{none,layer,row}"


def test_parse_help_empty_or_garbage_is_empty():
    assert parse_help("") == []
    assert parse_help("no flags here\njust prose\n") == []


def test_get_server_flags_no_binary_uses_bundled():
    out = get_server_flags(None)
    assert out["source"] == "bundled"
    assert out["flags"] is BUNDLED_FLAGS


def test_get_server_flags_falls_back_when_help_unparsable(monkeypatch, tmp_path):
    fake = tmp_path / "llama-server"
    fake.write_text("", encoding="utf-8")

    class FakeProc:
        stdout = "this is not help output"
        stderr = ""

    monkeypatch.setattr(flags.subprocess, "run", lambda *a, **k: FakeProc())
    out = get_server_flags(str(fake))
    assert out["source"] == "bundled"


def test_get_server_flags_parses_and_caches(monkeypatch, tmp_path):
    fake = tmp_path / "llama-server"
    fake.write_text("", encoding="utf-8")

    calls = {"n": 0}

    class FakeProc:
        stdout = SAMPLE
        stderr = ""

    def fake_run(*a, **k):
        calls["n"] += 1
        return FakeProc()

    monkeypatch.setattr(flags.subprocess, "run", fake_run)
    flags._cache.clear()

    out = get_server_flags(str(fake))
    assert out["source"] == "help"
    assert any(e["flags"] == ["-c", "--ctx-size"] for e in out["flags"])

    # Second call for the same (path, mtime) is served from cache.
    get_server_flags(str(fake))
    assert calls["n"] == 1
