---
name: cli-testing
description: Verify llama-monitor behavior with its bundled CLI using isolated Compose or native test environments. Apply to backend and integration changes; retain browser checks for UI behavior.
---

# Verify with the llama-monitor CLI

Use the CLI to verify runtime behavior covered by the task. Inspect JSON and exit
codes; do not infer success from a process starting or from watching the dashboard.
Read the repository README's **Command-line verification** section for command
options and the backend API contract.

## Default: isolated Compose smoke

Run appropriate automated tests first (`python -m pytest -q tests`, or a focused subset
during development). Then run `python tests/container_smoke.py`. This harness
builds the current checkout, starts a unique CPU Compose project with temporary
models, separate state, and free host ports, and exercises the CLI inside the
image against a fake inference process. It asserts both successful and failing
exit codes and cleans up only its own project and volume. Use this workflow for
ordinary lifecycle, telemetry, logs, history, and container changes. Run
`python tests/container_smoke.py --gpu` only when NVIDIA integration is relevant
and available. Neither fake-server test proves real model or GPU compatibility.

For manual checks against a task-owned Compose project, invoke:

```sh
docker compose -p TASK_PROJECT -f compose.yaml exec -T monitor python cli.py status --json
```

Keep the same project/file arguments for every command and cleanup. Include
`-f compose.override.yaml` when the project uses NVIDIA. The normal installed
application uses `docker compose exec -T monitor python cli.py ...`; use that
installation only when the task calls for it. Rebuild after source changes.
The CLI is bundled with the backend and requires no Python on the host for these
manual commands. Run the Python smoke harness from the development environment.

## Model verification sequence

On a task-owned backend:

1. `wait --backend --timeout 30 --json`, then `status --json`.
2. Inspect `configs list --json` / `configs show NAME --json`, or supply an
   unsaved configuration with `launch --config-file PATH`. Use backend paths
   such as `/models/model.gguf` in Docker; use `--config-file -` for stdin.
3. `launch --config NAME --wait --timeout 300 --json`. Launch replaces the
   backend's currently managed model, so inspect ownership first. Standalone
   `wait --ready --timeout 300 --json` pins the first observed managed run.
4. If inference is relevant, `smoke --timeout 60 --json`, followed by applicable
   `stats --json`, `logs --tail 100 --json`, and `history list --limit 10 --json`
   assertions. History ingestion is asynchronous; use bounded polling for the
   expected new run, not a fixed sleep or an unrelated existing row.
   When testing history, check the new run with the model filter before calling
   `stats`, including after restart, so UI polling cannot mask collection issues.
5. Exercise `restart --wait --json` if relevant, then `stop --json` and verify
   the stopped state. Shut down only the test's backend/Compose project.

`status` exit 0 means the backend responded, including when the model is stopped.
Readiness requires the managed process to own the listener and return healthy.
Smoke sends one fixed 16-token chat request and accepts answer or reasoning text;
do not assert an exact answer or performance threshold. It does not verify
embedding-only servers or external watch targets. Only use existing real models
when the task calls for real inference; do not download models or interrupt an
unrelated deployment. A real model test can consume shared GPU resources.

JSON mode returns one `{ok, data}` or `{ok, error}` object. Exit codes: 0 success,
1 operation/verification failure, 2 input error, 3 connection/protocol failure,
4 timeout, 130 interrupted. Inspect state before repeating a timed-out mutation;
it may still complete. Never hide an unsuccessful check with an unconditional
retry. Missing Docker, GPU, model files, or permissions are unverified coverage.

## Native and UI coverage

For native checks, set `LLAMA_MONITOR_DATA_DIR` to a fresh task-specific directory
under `test-results`, choose unused backend/model ports, and start
`python app.py --test-mode --port PORT`. Use
`python cli.py --url http://127.0.0.1:PORT ...`. Test mode skips adoption,
gaming/startup automation and inherited watch targets, disables integration
changes, and stops its owned models on graceful shutdown. Preserve the process
handle and stop that backend explicitly after stopping its model; native normal
mode intentionally leaves launched models alive. Restore any shell environment
variables changed for testing. Never reuse normal native data for test mode.

For Windows integration behavior, run the relevant native automated tests.
Docker reports gaming/startup support as unavailable, and native test mode
deliberately disables their mutations. Do not claim either verifies real Apollo
streaming, Windows startup installation, or unattended boot.

For UI changes, also run the existing frontend checks and browser suite
(`pnpm format:check`, `pnpm build`, `pnpm test:e2e` in `frontend`). CLI verification
covers backend behavior; it does not establish layout or interaction correctness.
Report the actual checks, outcomes, and any unavailable real-hardware coverage.
