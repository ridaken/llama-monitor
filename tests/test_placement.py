import json

import pytest

from history import HistoryDB, LogFollower
from placement import LayerPlacement, device_identity, placement_response


def model_log(labels=("CPU", "CUDA0", "CUDA1"), output="CUDA1", path="main.gguf"):
    lines = [f"llama_model_loader: loaded meta data with 1 key-value pairs and 1 tensors from {path} (version GGUF V3)",
             "llama_model_load: using device CUDA0 (GPU A) (0000:01:00.0) - 1024 MiB free",
             "llama_model_load: using device CUDA1 (GPU B) (0000:02:00.0) - 1024 MiB free",
             f"print_info: n_layer = {len(labels)}"]
    lines += [f"load_tensors: layer {i:3d} assigned to device {label}, is_swa = 0" for i, label in enumerate(labels)]
    if output:
        lines.append(f"load_tensors: layer {len(labels)} assigned to device {output}, is_swa = 0")
    return "\n".join(lines) + "\n"


def parse(text):
    parser = LayerPlacement()
    for line in text.splitlines():
        parser.feed(line)
    return parser


def fit_mtp_log():
    # Same-file, no-allocation loads are emitted repeatedly by --fit. MTP is
    # included in n_layer_all, but excluded from the target's n_layer count.
    return ("cmn common_params_print_info: build 11804 (test)\n"
            "cmn common_init_: fitting params to device memory ...\n"
            + model_log(("CUDA1",) * 3) * 2
            + "common_params_fit_impl: id=0, n_layer=0, n_part=0\n"
            "common_fit_params: fitting params to free memory took 1.0 seconds\n"
            + model_log(output=None).replace("print_info: n_layer = 3\n",
                                            "print_info: n_layer = 3\nprint_info: n_layer_all = 5\n")
            + "load_tensors: layer 3 assigned to device CUDA0\n"
            "load_tensors: layer 4 assigned to device CPU\n"
            "load_tensors: layer 5 assigned to device CUDA1\n"
            "common_speculative_init_result: creating MTP draft context against the target model 'main.gguf'\n")


def test_fit_trials_and_fit_diagnostics_are_not_model_loads():
    parser = parse(fit_mtp_log())
    result = parser.snapshot("main.gguf")
    assert result["status"] == "available"
    assert result["total_layers"] == 3
    assert result["output_device"] == "CUDA1"
    assert {d["label"]: d["layers"] for d in result["devices"]} == {"CPU": 1, "CUDA0": 1, "CUDA1": 1}
    parser.feed("common_params_fit_impl: id=0, n_layer=99, n_part=0")
    assert parser.snapshot("main.gguf") == result


def test_fit_without_real_load_never_publishes_trial_counts():
    parser = parse("fitting params to device memory ...\n" + model_log())
    assert parser.snapshot()["status"] == "unavailable"
    parser.feed("fitting params to free memory took 1.0 seconds")
    assert parser.snapshot()["status"] == "unavailable"
    parser.feed(model_log())
    assert parser.snapshot()["status"] == "available"
    parser.feed("cmn common_param: common_params_print_info: build 11804 (new process)")
    assert parser.snapshot()["status"] == "unavailable"
    assert not parser.fitting


@pytest.mark.parametrize("prefix", ["print_info", "llama_model_print_info", "llm_load_print_meta"])
def test_supported_model_info_prefixes(prefix):
    assert parse(model_log().replace("print_info:", prefix + ":")).snapshot()["status"] == "available"


@pytest.mark.parametrize("total_all", [0, 2])
def test_mtp_all_layers_cannot_be_smaller_than_main_layers(total_all):
    assert parse(fit_mtp_log().replace("n_layer_all = 5", f"n_layer_all = {total_all}")).snapshot()["status"] == "unavailable"


@pytest.mark.parametrize("replacement,output,status", [
    ("load_tensors: layer 5 assigned to device CUDA1\n", None, "available"),
    ("load_tensors: layer   1 assigned to device CUDA0, is_swa = 0\n", None, "unavailable"),
    ("print_info: n_layer_all = 5", None, "unavailable"),
])
def test_mtp_missing_output_main_assignment_or_total(replacement, output, status):
    result = parse(fit_mtp_log().replace(replacement, "")).snapshot("main.gguf")
    assert result["status"] == status
    assert result.get("output_device") == output


@pytest.mark.parametrize("jsonl", [False, True])
def test_fit_mtp_follower_recovery_and_transaction_rollback(tmp_path, monkeypatch, jsonl):
    path = tmp_path / "server.log"
    path.write_text("", encoding="utf-8")
    db = HistoryDB(str(tmp_path / "history.sqlite"))
    follower = LogFollower(db, str(path), managed=True, run_id="run")
    log = fit_mtp_log()
    # Include a multi-line JSON message as emitted by some logging wrappers.
    path.write_text(json.dumps({"type": "log", "msg": log}) + "\n" if jsonl else log, encoding="utf-8")
    save = db.save_batch
    def fail(*args, **kwargs):
        raise RuntimeError("write failed")
    monkeypatch.setattr(db, "save_batch", fail)
    with pytest.raises(RuntimeError):
        follower.poll()
    assert not follower.placement.fitting
    assert not follower.placement.blocks
    monkeypatch.setattr(db, "save_batch", save)
    follower.poll()
    result = follower.snapshot()["layer_parser"].snapshot("main.gguf")
    assert result["status"] == "available" and result["total_layers"] == 3
    resumed = LogFollower(db, str(path), managed=True, run_id="run")
    assert resumed.snapshot()["layer_parser"].snapshot("main.gguf") == result


@pytest.mark.parametrize("labels", [("CPU",) * 3, ("CUDA0",) * 3, ("CPU", "CUDA0", "CUDA1")])
def test_complete_assignments_exclude_output_and_confirm_zero(labels):
    parser = parse(model_log(labels))
    result = parser.snapshot("main.gguf")
    assert result["status"] == "available"
    assert result["total_layers"] == 3
    assert result["output_device"] == "CUDA1"
    counts = {d["label"]: d["layers"] for d in result["devices"]}
    assert sum(counts.values()) == 3
    assert counts["CPU"] == labels.count("CPU")


def test_duplicates_missing_conflicting_and_invalid_layers():
    text = model_log()
    duplicate = "load_tensors: layer 1 assigned to device CUDA0\n"
    assert parse(text + duplicate).snapshot()["status"] == "available"
    assert parse(text.replace("load_tensors: layer   1", "ignored: layer   1")).snapshot()["status"] == "unavailable"
    assert parse(text + duplicate.replace("CUDA0", "CUDA1")).snapshot()["status"] == "unavailable"
    assert parse(text + "load_tensors: layer 99 assigned to device CPU").snapshot()["status"] == "unavailable"


def test_draft_projector_and_model_replacement_do_not_merge():
    parser = parse(model_log() + model_log(("CPU",), "CPU", "draft.gguf"))
    assert parser.snapshot()["status"] == "unavailable"
    assert parser.snapshot("main.gguf")["total_layers"] == 3
    assert parser.snapshot("new.gguf")["status"] == "unavailable"
    parser = parse(model_log() + model_log(("CPU",), "CPU", "main.gguf"))
    # Same file can be loaded for both main and draft; do not guess its role.
    assert parser.snapshot("main.gguf")["status"] == "unavailable"
    parser.feed("build: 2 (replacement)")
    for message in model_log(("CPU",), "CPU", "main.gguf").splitlines():
        parser.feed(message)
    assert parser.snapshot("main.gguf")["total_layers"] == 1


def test_shared_draft_does_not_change_main_assignment():
    parser = parse(model_log() + model_log(("CPU",), "CPU", "draft.gguf") + "split_mode = row")
    assert parser.snapshot("main.gguf")["status"] == "available"
    assert parser.snapshot("draft.gguf")["status"] == "shared"


def test_malformed_large_total_is_unavailable_without_allocating_a_layer_range():
    assert parse("print_info: n_layer = 999999999999999999\n").snapshot()["status"] == "unavailable"


def test_timestamped_restart_banner_and_missing_output_layer():
    parser = parse(model_log(output=None))
    assert parser.snapshot()["status"] == "available"
    assert parser.snapshot()["output_device"] is None
    parser.feed("0.00.000.100 I main: build = 9999 (new)")
    assert parser.snapshot()["status"] == "unavailable"


def test_identity_matching_never_uses_cuda_ordinal():
    gpu = {"devices": [{"index": 7, "pci_bus_id": "00000000:01:00.0"}, {"index": 0}]}
    result = placement_response(parse(model_log()), gpu, online=True)
    devices = {d["label"]: d for d in result["devices"]}
    assert devices["CUDA0"]["gpu_index"] == 7
    assert "gpu_index" not in devices["CUDA1"]
    assert device_identity("GPU-ABC") == "gpu-abc"
    assert device_identity("unknown id") is None


def test_memory_identity_matching_does_not_require_layer_assignments():
    parser = parse("llama_model_load: using device CUDA0 (GPU A) (0000:01:00.0) - 1024 MiB free\n")
    gpu = {"devices": [{"index": 7, "pci_bus_id": "00000000:01:00.0"}, {"index": 0}]}
    assert parser.snapshot()["status"] == "unavailable"
    assert parser.device_indices(gpu) == {"CUDA0": 7}
    assert parser.device_indices({"devices": [{"index": 0}]}) == {}
    parser.feed("llama_model_load: using device CUDA0 (GPU B) (0000:02:00.0) - 1024 MiB free")
    assert parser.device_indices(gpu) == {}
    parser.feed("build: 2 (replacement)")
    assert parser.device_indices(gpu) == {}


@pytest.mark.parametrize("state,online,expected", [("running", False, "loading"), ("stopped", True, "no_model"), ("exited", True, "no_model"), (None, False, "no_model")])
def test_lifecycle_suppresses_stale_cached_counts(state, online, expected):
    result = placement_response(parse(model_log()), {}, online=online, managed_state=state)
    assert result["status"] == expected and not result["devices"]


@pytest.mark.parametrize("mode", ["row", "tensor"])
def test_shared_placement_from_logs_config_and_environment(monkeypatch, mode):
    assert parse(model_log() + f"split_mode = {mode}").snapshot()["status"] == "shared"
    config = {"flags": [{"flag": "-sm", "value": mode}]}
    assert placement_response(parse(model_log()), {}, online=True, config=config)["status"] == "shared"
    result = placement_response(LayerPlacement(), {}, online=True, config=config)
    assert result["status"] == "shared" and result["devices"] == []
    assert "Restart" not in result["reason"]
    monkeypatch.setenv("LLAMA_ARG_SPLIT_MODE", mode)
    assert placement_response(parse(model_log()), {}, online=True, managed_state="running")["status"] == "shared"
    config = {"flags": [{"flag": "--split-mode", "value": "layer"}]}
    assert placement_response(parse(model_log()), {}, online=True, managed_state="running", config=config)["status"] == "available"


def test_follower_jsonl_resume_rotation_and_rollback(tmp_path, monkeypatch):
    path = tmp_path / "server.log"
    path.write_text("", encoding="utf-8")
    db = HistoryDB(str(tmp_path / "history.sqlite"))
    follower = LogFollower(db, str(path), managed=True, run_id="run")
    text = "".join(json.dumps({"type": "log", "msg": msg}) + "\n" for msg in model_log().splitlines())
    path.write_text(text, encoding="utf-8")
    save = db.save_batch
    def fail(*args, **kwargs):
        raise RuntimeError("write failed")
    monkeypatch.setattr(db, "save_batch", fail)
    with pytest.raises(RuntimeError):
        follower.poll()
    assert follower.snapshot()["layer_parser"].snapshot()["status"] == "unavailable"
    monkeypatch.setattr(db, "save_batch", save)
    follower.poll()
    assert follower.snapshot()["layer_parser"].snapshot()["total_layers"] == 3
    resumed = LogFollower(db, str(path), managed=True, run_id="run")
    assert resumed.snapshot()["layer_parser"].snapshot()["total_layers"] == 3
    with path.open("a", encoding="utf-8") as f:
        f.write("build: 2 (new)\n")
    resumed.poll()
    assert resumed.snapshot()["layer_parser"].snapshot()["status"] == "unavailable"
    path.write_text(model_log(("CPU",), "CPU"), encoding="utf-8")
    resumed.poll()
    assert resumed.snapshot()["layer_parser"].snapshot()["total_layers"] == 1
