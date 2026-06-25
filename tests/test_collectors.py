"""Tests for the parsing logic in collectors.py.

These parsers are deliberately tolerant of llama.cpp log/metric wording, which
also means a regression can silently blank out a panel rather than error — so
the parsers are the most valuable thing to lock down.
"""

import collectors
from collectors import (
    LlamaCollector,
    LogTailer,
    parse_device_baseline,
    parse_device_split,
    parse_prometheus,
    parse_spec_enabled,
)


# --------------------------------------------------------------------------- #
# Prometheus metrics                                                           #
# --------------------------------------------------------------------------- #

def test_parse_prometheus_extracts_values_and_ignores_noise():
    text = (
        "# HELP llamacpp:predicted_tokens_seconds help\n"
        "# TYPE llamacpp:predicted_tokens_seconds gauge\n"
        "llamacpp:predicted_tokens_seconds 42.5\n"
        "llamacpp:kv_cache_usage_ratio 0.5\n"
        "this is not a metric line\n"
        "\n"
    )
    out = parse_prometheus(text)
    assert out["llamacpp:predicted_tokens_seconds"] == 42.5
    assert out["llamacpp:kv_cache_usage_ratio"] == 0.5
    assert "this" not in out
    assert len(out) == 2


# --------------------------------------------------------------------------- #
# Per-device memory split from the startup log                                 #
# --------------------------------------------------------------------------- #

def test_parse_device_split_sums_and_orders(tmp_path):
    log = tmp_path / "llama.log"
    log.write_text(
        "build: 100 (abc) with gcc\n"
        "load_tensors:        CUDA0 model buffer size =  8500.00 MiB\n"
        "load_tensors:        CUDA1 model buffer size =  7000.00 MiB\n"
        "llama_kv_cache:      CUDA0 KV buffer size =  300.00 MiB\n"
        "load_tensors:   CPU_Mapped model buffer size = 8123.45 MiB\n"
        "ggml_cuda_host_malloc: CUDA_Host buffer size = 12.00 MiB\n",
        encoding="utf-8",
    )
    split = parse_device_split(str(log))
    # CUDA0 = model 8500 + KV 300; host-side buffers fold into CPU.
    assert split["CUDA0"] == 8800.0
    assert split["CUDA1"] == 7000.0
    assert round(split["CPU"], 2) == 8135.45
    # Ordering: CUDA0, CUDA1, then CPU last.
    assert list(split.keys()) == ["CUDA0", "CUDA1", "CPU"]


def test_parse_device_split_uses_only_latest_run(tmp_path):
    log = tmp_path / "llama.log"
    log.write_text(
        "build: 1 (old) with gcc\n"
        "load_tensors: CUDA0 model buffer size = 9999.00 MiB\n"
        "build: 2 (new) with gcc\n"
        "load_tensors: CUDA0 model buffer size = 100.00 MiB\n",
        encoding="utf-8",
    )
    split = parse_device_split(str(log))
    assert split == {"CUDA0": 100.0}


def test_parse_device_split_missing_file_is_empty():
    assert parse_device_split("/no/such/file.log") == {}


def test_parse_device_baseline_reads_free_memory(tmp_path):
    log = tmp_path / "llama.log"
    log.write_text(
        "some preamble\n"
        "device_info:\n"
        "- CUDA0   : NVIDIA RTX (12281 MiB, 11069 MiB free)\n"
        "- CUDA1   : NVIDIA RTX (12281 MiB, 10000 MiB free)\n",
        encoding="utf-8",
    )
    base = parse_device_baseline(str(log))
    assert base == {"CUDA0": 11069.0, "CUDA1": 10000.0}


def test_parse_spec_enabled(tmp_path):
    on = tmp_path / "on.log"
    on.write_text("loading draft model for draft-mtp speculation\n", encoding="utf-8")
    off = tmp_path / "off.log"
    off.write_text("ordinary startup with no speculation\n", encoding="utf-8")
    assert parse_spec_enabled(str(on)) is True
    assert parse_spec_enabled(str(off)) is False


# --------------------------------------------------------------------------- #
# Slot state machine                                                           #
# --------------------------------------------------------------------------- #

def _collector():
    # Constructing is cheap and makes no network call.
    return LlamaCollector("http://127.0.0.1:9")


def test_slot_detail_idle():
    d = _collector()._slot_detail({"id": 0, "is_processing": False}, n_ctx=1000)
    assert d["state"] == "idle"
    assert d["decoded"] == 0


def test_slot_detail_prefill_reports_progress_ratio():
    sl = {"id": 1, "is_processing": True,
          "n_prompt_tokens": 100, "n_prompt_tokens_processed": 40}
    d = _collector()._slot_detail(sl, n_ctx=1000)
    assert d["state"] == "prefill"
    assert d["prefill_ratio"] == 0.4


def test_slot_detail_generating_counts_context():
    sl = {"id": 2, "is_processing": True, "n_prompt_tokens": 100,
          "next_token": [{"n_decoded": 10}]}
    d = _collector()._slot_detail(sl, n_ctx=1000)
    assert d["state"] == "generating"
    assert d["decoded"] == 10
    assert d["ctx_used"] == 110
    assert d["ctx_ratio"] == 0.11


# --------------------------------------------------------------------------- #
# Log tailer: activity detection + exact per-request timings                   #
# --------------------------------------------------------------------------- #

def test_log_tailer_detects_activity_and_idle(tmp_path):
    log = tmp_path / "llama.log"
    log.write_text("startup\n", encoding="utf-8")
    tailer = LogTailer(str(log))  # starts tailing at end of file
    assert tailer.poll() is None  # nothing new yet

    with open(log, "a", encoding="utf-8") as f:
        f.write("slot launch_slot: id 0 | processing task\n")
    assert tailer.poll() is True

    with open(log, "a", encoding="utf-8") as f:
        f.write("update_slots: all slots are idle\n")
    assert tailer.poll() is False


def test_log_tailer_parses_prefill_and_decode_timings(tmp_path):
    log = tmp_path / "llama.log"
    log.write_text("", encoding="utf-8")
    tailer = LogTailer(str(log))
    with open(log, "a", encoding="utf-8") as f:
        f.write("prompt eval time =  100.00 ms /    50 tokens\n")
        f.write("       eval time =  200.00 ms /   100 tokens\n")
    tailer.poll()

    assert tailer.last["prefill"]["tokens"] == 50
    assert tailer.last["prefill"]["tps"] == 500.0       # 50 tok / 0.1 s
    # The "prompt eval time" line must NOT be misread as the decode timing.
    assert tailer.last["decode"]["tokens"] == 100
    assert tailer.last["decode"]["tps"] == 500.0        # 100 tok / 0.2 s


def test_log_tailer_parses_total_time(tmp_path):
    """The exact pp + generation = total breakdown for the last request, in the
    real print_timing wording."""
    log = tmp_path / "llama.log"
    log.write_text("", encoding="utf-8")
    tailer = LogTailer(str(log))
    with open(log, "a", encoding="utf-8") as f:
        f.write("slot print_timing: id 3 | prompt eval time =  1033.51 ms /   11 tokens\n")
        f.write("slot print_timing: id 3 |        eval time = 12527.01 ms /  334 tokens\n")
        f.write("slot print_timing: id 3 |       total time = 13560.52 ms /  345 tokens\n")
    tailer.poll()

    assert tailer.last["prefill"]["secs"] == 1.03351
    assert tailer.last["decode"]["tokens"] == 334
    assert tailer.last["total"]["tokens"] == 345
    assert tailer.last["total"]["secs"] == 13.56052
    # The three are self-consistent: pp + generation == total.
    pp, gen, total = (tailer.last[k]["secs"] for k in ("prefill", "decode", "total"))
    assert round(pp + gen, 5) == round(total, 5)


def test_log_tailer_handles_truncation(tmp_path):
    log = tmp_path / "llama.log"
    log.write_text("line one\nline two\n", encoding="utf-8")
    tailer = LogTailer(str(log))
    # Rotate/truncate: new file shorter than the old read position.
    log.write_text("fresh\n", encoding="utf-8")
    # Should not crash and should re-read from the top rather than skipping.
    assert tailer.poll() is not None
