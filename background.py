"""File logging for a console-free pythonw backend launched by Task Scheduler."""
from pathlib import Path


def log_config(directory):
    Path(directory).mkdir(parents=True, exist_ok=True)
    return {"version": 1, "disable_existing_loggers": False,
            "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(message)s"}},
            "handlers": {"file": {"class": "logging.handlers.RotatingFileHandler",
                "formatter": "plain", "filename": str(Path(directory) / "backend.log"),
                "maxBytes": 1024 * 1024, "backupCount": 3, "encoding": "utf-8"}},
            "root": {"handlers": ["file"], "level": "WARNING"},
            "loggers": {"uvicorn": {"handlers": ["file"], "level": "INFO", "propagate": False},
                        "uvicorn.error": {"level": "INFO"},
                        "llama_monitor": {"handlers": ["file"], "level": "INFO", "propagate": False}}}
