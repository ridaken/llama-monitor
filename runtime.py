"""Deployment settings shared by the native and container entry points."""
import ntpath
import os
import posixpath


def containerized() -> bool:
    return os.environ.get("LLAMA_MONITOR_CONTAINER") == "1"


def models_host_path() -> str | None:
    """Compose snapshots the same source used for the /models bind mount."""
    if not containerized():
        return None
    source = os.environ.get("MONITOR_MODELS_HOST_PATH", "")
    drive, _ = ntpath.splitdrive(source)
    if drive and ntpath.isabs(source):
        return ntpath.normpath(source)
    if posixpath.isabs(source):
        return posixpath.normpath(source)
    # Relative Compose paths cannot be resolved against the container's cwd.
    return None


def info() -> dict:
    return {"containerized": containerized(),
            "models_dir": "/models" if containerized() else None,
            "models_host_path": models_host_path(),
            "managed_port": 8001 if containerized() else None,
            "embedding_port": 8081 if containerized() else None,
            "embedding_published_port": int(os.environ.get("MONITOR_EMBEDDING_PUBLISHED_PORT", "8081")) if containerized() else None}
