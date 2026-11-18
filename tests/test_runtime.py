"""The displayed host folder uses the source captured when Compose creates it."""
import pytest

import runtime


@pytest.mark.parametrize("source, expected", [
    ("/home/tom/models", "/home/tom/models"),
    ("/srv/AI models/../models/", "/srv/models"),
    ("C:/Users/Tom/AI Models", "C:\\Users\\Tom\\AI Models"),
    (r"D:\AI Models", r"D:\AI Models"),
    (r"\\wsl.localhost\Ubuntu\home\tom\models", r"\\wsl.localhost\Ubuntu\home\tom\models"),
    ("./models", None),
    ("models", None),
    ("C:models", None),
    ("", None),
])
def test_models_host_path(source, expected, monkeypatch):
    monkeypatch.setenv("LLAMA_MONITOR_CONTAINER", "1")
    monkeypatch.setenv("MONITOR_MODELS_HOST_PATH", source)
    assert runtime.info()["models_host_path"] == expected


def test_runtime_uses_container_snapshot_not_changed_host_environment(monkeypatch):
    monkeypatch.setenv("LLAMA_MONITOR_CONTAINER", "1")
    monkeypatch.setenv("MONITOR_MODELS_HOST_PATH", "/home/tom/models")
    monkeypatch.setenv("MODELS_DIR", "/different/folder")
    assert runtime.info()["models_host_path"] == "/home/tom/models"


def test_runtime_without_mount_source(monkeypatch):
    monkeypatch.setenv("LLAMA_MONITOR_CONTAINER", "1")
    monkeypatch.delenv("MONITOR_MODELS_HOST_PATH", raising=False)
    assert runtime.info()["models_host_path"] is None


def test_native_runtime_does_not_report_container_mount(monkeypatch):
    monkeypatch.delenv("LLAMA_MONITOR_CONTAINER", raising=False)
    monkeypatch.setenv("MONITOR_MODELS_HOST_PATH", "/home/tom/models")
    assert runtime.info()["models_host_path"] is None
