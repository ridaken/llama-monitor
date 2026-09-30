"""Serve the real FastAPI app with isolated state for browser tests."""

import argparse
import os
import sys

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)

import uvicorn  # noqa: E402
import app as app_module  # noqa: E402
import store  # noqa: E402


def main():
    port = int(os.environ.get("UI_TEST_PORT", "8765"))
    state_dir = os.path.join(root, "frontend", "test-results", f"server-state-{port}")
    os.makedirs(state_dir, exist_ok=True)
    store.HOME_DIR = state_dir
    store.STATE_PATH = os.path.join(state_dir, "state.json")
    store.MANAGED_LOG = os.path.join(state_dir, "llama-server.log")
    store.PROMPTS_DIR = os.path.join(state_dir, "prompts")
    args = argparse.Namespace(
        llama_url="http://127.0.0.1:9", llama_log=None,
        llama_prompts_dir=None, port=port, host="127.0.0.1",
    )
    uvicorn.run(app_module.build_app(args), host="127.0.0.1", port=port, log_level="error")


if __name__ == "__main__":
    main()
