"""History is committed from complete log lines and survives monitor restarts."""

import json
import os
import time

import pytest

from history import HistoryDB, LogFollower, decode_log_line


def line(slot, message, task=1):
    return f"slot print_timing: id {slot} | task {task} | {message}\n"


def generation(slot=0, task=1, prompt=12, generated=7, total=300):
    return (f"slot launch_slot: id {slot} | processing task {task}\n"
            + line(slot, f"prompt eval time = 100.00 ms / {prompt} tokens", task)
            + line(slot, f"eval time = 200.00 ms / {generated} tokens", task)
            + line(slot, f"total time = {total:.2f} ms / {prompt + generated} tokens", task))


def make(tmp_path, *, managed=True, exists=True):
    path = tmp_path / "server.log"
    if exists:
        path.write_bytes(b"")
    db = HistoryDB(str(tmp_path / "history.sqlite"))
    follower = LogFollower(db, str(path), run_id="managed-run" if managed else None,
                           managed=managed)
    return path, db, follower


def test_text_and_jsonl_partial_and_malformed(tmp_path):
    path, db, follower = make(tmp_path)
    assert decode_log_line('{"type":"log","msg":"hello"}') == "hello"
    assert decode_log_line('{"type":"future","msg":"hello"}') is None
    assert decode_log_line('{broken') is None
    text = generation()
    first, last = text.rsplit("\n", 2)[0] + "\n", text.rsplit("\n", 2)[1]
    with path.open("ab") as f:
        f.write(first.encode())
        f.write(b"{broken\n")
        f.write(json.dumps({"type": "future", "msg": "ignored"}).encode() + b"\n")
        f.write(last[:20].encode())
    follower.poll()
    assert db.list()["items"][0]["state"] == "running"
    assert follower.offset < path.stat().st_size
    with path.open("ab") as f:
        f.write(last[20:].encode() + b"\n")
    follower.poll()
    item = db.list()["items"][0]
    assert item["state"] == "complete"
    assert item["prompt_tokens"] == 12
    assert item["generated_tokens"] == 7
    assert item["total_seconds"] == pytest.approx(.3)

    json_log = tmp_path / "json.log"
    json_log.write_bytes(b"")
    jf = LogFollower(db, str(json_log), run_id="json-run", managed=True)
    with json_log.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "log", "msg":
            "load_tensors: CUDA0 model buffer size = 100.00 MiB"}) + "\n")
        for raw in generation(slot=2).splitlines():
            f.write(json.dumps({"type": "log", "msg": raw}) + "\n")
    jf.poll()
    assert db.latest_complete("json-run")["slot_id"] == 2
    assert jf.snapshot()["split"]["CUDA0"] == 100


def test_concurrent_slots_and_run_reuse(tmp_path):
    path, db, follower = make(tmp_path)
    with path.open("a", encoding="utf-8") as f:
        f.write("slot launch_slot: id 0 | processing task 1\n")
        f.write("slot launch_slot: id 1 | processing task 2\n")
        f.write(line(0, "prompt eval time = 100 ms / 12 tokens", 1))
        f.write(line(1, "prompt eval time = 100 ms / 30 tokens", 2))
        f.write(line(1, "eval time = 200 ms / 9 tokens", 2))
        f.write(line(0, "eval time = 200 ms / 7 tokens", 1))
        f.write(line(1, "total time = 300 ms / 39 tokens", 2))
        f.write(line(0, "total time = 300 ms / 19 tokens", 1))
    follower.poll()
    items = db.list()["items"]
    assert len(items) == 2
    assert {(i["slot_id"], i["task_id"], i["prompt_tokens"], i["generated_tokens"])
            for i in items} == {(0, 1, 12, 7), (1, 2, 30, 9)}
    before = {i["id"] for i in items}
    restarted = LogFollower(db, str(path), run_id="managed-run", managed=True)
    restarted.poll()
    assert {i["id"] for i in db.list()["items"]} == before
    with path.open("a", encoding="utf-8") as f:
        f.write(generation(slot=0, task=1, prompt=4))
    restarted.poll()
    assert len(db.list()["items"]) == 3


def test_missing_external_resume_and_rotation(tmp_path):
    path = tmp_path / "external.log"
    db = HistoryDB(str(tmp_path / "history.sqlite"))
    follower = LogFollower(db, str(path))
    follower.poll()
    assert not follower.snapshot()["available"]
    path.write_text(generation(), encoding="utf-8")
    follower.poll()  # first external attachment starts at EOF
    assert db.list()["items"] == []
    with path.open("a", encoding="utf-8") as f:
        f.write(generation(slot=1))
    follower.poll()
    assert len(db.list()["items"]) == 1
    old_run = follower.run_id
    path.write_text(generation(slot=2), encoding="utf-8")
    follower.poll()
    assert follower.snapshot()["gap"]
    assert follower.run_id != old_run


def test_missing_file_closes_open_generation_and_recovers(tmp_path):
    path, db, follower = make(tmp_path)
    path.write_text("slot launch_slot: id 0 | processing task 1\n", encoding="utf-8")
    follower.poll()
    assert db.list()["items"][0]["state"] == "running"
    os.unlink(path)
    follower.poll()
    assert follower.snapshot()["gap"]
    assert db.list()["items"][0]["state"] == "incomplete"
    restarted_missing = LogFollower(db, str(path), run_id="managed-run", managed=True)
    assert restarted_missing.snapshot()["gap"]
    path.write_text(generation(slot=1), encoding="utf-8")
    follower.poll()
    assert follower.run_id == "managed-run"
    assert len(db.list()["items"]) == 2


def test_replaced_file_with_reused_inode_is_detected(tmp_path, monkeypatch):
    # Some filesystems reuse the inode immediately after unlink/recreate.
    monkeypatch.setattr(LogFollower, "_key", staticmethod(lambda stat: "same-inode"))
    path, db, follower = make(tmp_path)
    path.write_text("slot launch_slot: id 0 | processing task 1\n", encoding="utf-8")
    follower.poll()
    os.unlink(path)
    follower.poll()
    path.write_text(generation(slot=1), encoding="utf-8")
    follower.poll()
    rows = db.list()["items"]
    assert len(rows) == 2
    assert {r["slot_id"] for r in rows} == {0, 1}
    assert db.get_cursor(str(path))["offset"] == path.stat().st_size


def test_failed_commit_retries_same_lines(tmp_path, monkeypatch):
    path, db, follower = make(tmp_path)
    path.write_text(generation(), encoding="utf-8")
    old_save = db.save_batch
    calls = 0
    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary database failure")
        return old_save(*args, **kwargs)
    monkeypatch.setattr(db, "save_batch", fail_once)
    with pytest.raises(RuntimeError):
        follower.poll()
    assert follower.offset == 0
    assert db.list()["items"] == []
    follower.poll()
    assert len(db.list()["items"]) == 1


def test_unattributed_timing_is_ambiguous(tmp_path):
    path, db, follower = make(tmp_path)
    path.write_text("prompt eval time = 100 ms / 12 tokens\n", encoding="utf-8")
    follower.poll()
    item = db.list()["items"][0]
    assert item["state"] == "ambiguous"
    assert item["slot_id"] is None
    assert db.latest_complete("managed-run") is None


def test_finishing_server_marks_open_generation_incomplete(tmp_path):
    path, db, follower = make(tmp_path)
    path.write_text("slot launch_slot: id 0 | processing task 1\n", encoding="utf-8")
    follower.finish_segment()
    assert db.list()["items"][0]["state"] == "incomplete"


def test_current_speculative_mean_len_wording(tmp_path):
    path, db, follower = make(tmp_path)
    with path.open("a", encoding="utf-8") as f:
        f.write(generation(slot=3, task=4))
        f.write(line(3, "draft acceptance = 1.00000 (20 accepted / 20 generated), mean len = 3.00", 4))
    follower.poll()
    item = db.latest_complete("managed-run")
    assert item["draft_accepted"] == 20
    assert item["draft_generated"] == 20
    assert item["draft_mean_len"] == 3.0


def test_external_build_banner_starts_new_run(tmp_path):
    path = tmp_path / "external.log"
    path.write_bytes(b"")
    db = HistoryDB(str(tmp_path / "history.sqlite"))
    follower = LogFollower(db, str(path))
    with path.open("a", encoding="utf-8") as f:
        f.write(generation(task=1))
    follower.poll()
    old_run = follower.run_id
    with path.open("a", encoding="utf-8") as f:
        f.write("build: fresh server\n")
        f.write(generation(task=1))
    follower.poll()
    assert follower.run_id != old_run
    assert {i["run_id"] for i in db.list()["items"]} == {old_run, follower.run_id}


def test_filters_pagination_and_manual_clear_keep_cursor(tmp_path):
    path, db, follower = make(tmp_path)
    follower.set_model("model-a")
    with path.open("a", encoding="utf-8") as f:
        f.write(generation(slot=0, task=1, prompt=2, generated=3, total=100))
        f.write(generation(slot=1, task=2, prompt=4, generated=5, total=200))
        f.write("slot launch_slot: id 2 | processing task 3\n")
        f.write(line(2, "prompt eval time = 100 ms / 6 tokens", 3))
    follower.poll()
    page = db.list(limit=1, sort="prompt_tokens", order="asc")
    assert page["items"][0]["prompt_tokens"] == 2
    following = db.list(limit=1, sort="prompt_tokens", order="asc",
                        cursor=page["next_cursor"])
    assert following["items"][0]["prompt_tokens"] == 4
    assert len(db.list(state="complete", model="model-a")["items"]) == 2
    assert len(db.list(state="running")["items"]) == 1
    assert db.list(from_ts=9999999999)["items"] == []
    cursor = db.get_cursor(str(path))["offset"]
    assert follower.clear_history() == 3
    assert db.list()["items"] == []
    assert db.get_cursor(str(path))["offset"] == cursor
    restarted = LogFollower(db, str(path), run_id="managed-run", managed=True)
    restarted.poll()
    assert db.list()["items"] == []


def test_invalid_cursor_and_sort(tmp_path):
    db = HistoryDB(str(tmp_path / "history.sqlite"))
    with pytest.raises(ValueError):
        db.list(sort="bogus")
    with pytest.raises(ValueError):
        db.list(cursor="bogus")


def test_native_prompt_file_is_saved_linked_and_survives_restart(tmp_path):
    log = tmp_path / "server.log"
    log.write_bytes(b"")
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    db = HistoryDB(str(tmp_path / "history.sqlite"))
    started = time.time() - 2
    follower = LogFollower(db, str(log), run_id="prompt-run", managed=True,
                           started_at=started, prompt_dir=str(prompt_dir))
    prompt = prompt_dir / "000123456789.txt"
    prompt.write_text("<|im_start|>user\nHello\n<|im_end|>", encoding="utf-8")
    os.utime(prompt, (time.time() - 1, time.time() - 1))
    follower.poll()
    follower.poll()  # native file must be unchanged across two scans
    only = db.list()["items"]
    assert len(only) == 1 and only[0]["state"] == "prompt_only"
    assert "Hello" in db.get_prompt(only[0]["id"])["prompt_text"]
    log.write_text(generation(), encoding="utf-8")
    follower.poll()
    db.match_prompts("prompt-run", now=time.time() + 6)
    item = db.list()["items"]
    assert len(item) == 1 and item[0]["state"] == "complete"
    assert item[0]["has_prompt"] == 1
    assert "Hello" in db.get_prompt(item[0]["id"])["prompt_text"]
    restarted = LogFollower(db, str(log), run_id="prompt-run", managed=True,
                            started_at=started, prompt_dir=str(prompt_dir))
    restarted.poll()
    assert len(db.list()["items"]) == 1
    pending = prompt_dir / "000123456790.txt"
    pending.write_text("not yet scanned", encoding="utf-8")
    os.utime(pending, (time.time() - 1, time.time() - 1))
    assert restarted.clear_history() == 1
    restarted.poll()
    restarted.poll()
    assert db.list()["items"] == []
    assert not prompt.exists()
    assert db.list()["items"] == []  # even an unseen native file cannot replay


def test_overlapping_native_prompts_are_left_unmatched(tmp_path):
    log = tmp_path / "server.log"
    log.write_bytes(b"")
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    db = HistoryDB(str(tmp_path / "history.sqlite"))
    follower = LogFollower(db, str(log), run_id="concurrent", managed=True,
                           started_at=time.time() - 2, prompt_dir=str(prompt_dir))
    for n in (1, 2):
        prompt = prompt_dir / f"{n:012}.txt"
        prompt.write_text(f"prompt {n}", encoding="utf-8")
        os.utime(prompt, (time.time() - 1, time.time() - 1))
    log.write_text(generation(slot=0, task=1) + generation(slot=1, task=2),
                   encoding="utf-8")
    follower.poll()
    follower.poll()
    db.match_prompts("concurrent", now=time.time() + 6)
    rows = db.list()["items"]
    assert len(rows) == 4
    assert sum(row["state"] == "prompt_only" for row in rows) == 2
    assert all(not row["has_prompt"] for row in rows if row["state"] == "complete")
