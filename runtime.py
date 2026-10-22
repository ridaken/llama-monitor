"""Deployment settings shared by the native and container entry points."""
import os


def containerized() -> bool:
    return os.environ.get("LLAMA_MONITOR_CONTAINER") == "1"


def info() -> dict:
    return {"containerized": containerized(),
            "models_dir": "/models" if containerized() else None,
            "managed_port": 8001 if containerized() else None}
