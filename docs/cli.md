# CLI reference

The CLI is a noninteractive client for an already running llama-monitor backend.
It uses the same HTTP controls as the dashboard. It does not start the backend,
install llama-server, or install Windows integrations.

- [Connection and global options](#connection-and-global-options)
- [Commands](#commands)
- [Configuration JSON](#configuration-json)
- [Examples](#examples)
- [Output and exit codes](#output-and-exit-codes)
- [Timeouts and verification](#timeouts-and-verification)

See the [README](../README.md#command-line-verification) for a quick start and the
[HTTP API reference](api.md) for response fields and endpoint contracts.

## Connection and global options

Native installation, from the checkout and its Python environment:

```sh
python cli.py [GLOBAL_OPTIONS] COMMAND [COMMAND_OPTIONS]
```

Docker includes Python and the CLI:

```sh
docker compose exec -T monitor python cli.py status --json
# CPU-only Compose installation:
docker compose -f compose.yaml exec -T monitor python cli.py status --json
```

Use the same project/file selection as the backend you intend to control. Inside
the container, the default URL works regardless of published host ports. Rebuild
the image after source changes to exercise the updated CLI and backend.

| Option | Default | Behavior |
| --- | --- | --- |
| `--url URL` | `LLAMA_MONITOR_URL`, otherwise `http://127.0.0.1:8500` | Backend URL; the explicit option takes precedence. Must be HTTP(S), without credentials, query, or fragment. |
| `--json` | Off | Write exactly one result object to stdout, including on handled failures. |
| `--request-timeout SECONDS` | `10` | Positive finite per-request timeout; see the wait and smoke exceptions below. |
| `-h`, `--help` | — | Print help and exit without connecting. Available at each command level. |

Global options can appear before or after subcommands. Requests ignore inherited
HTTP proxy settings and do not follow redirects. Point the client directly at
the backend. The backend has no built-in dashboard authentication; use the
[trusted-network deployment guidance](../README.md#install-with-docker-compose-recommended).

## Commands

All examples below can be prefixed with `python cli.py` or the Docker invocation
above. Results are the referenced API response in `data` when using `--json`,
except for the aggregation performed by `status` and additions from `--wait`.

| Command and options | Result and effects |
| --- | --- |
| `status` | Reads health, launcher state, and fresh managed readiness; returns `{backend, launcher, readiness}`. Exit 0 means the backend answered, even with a stopped or unready model. Readiness can contact the inference health endpoint. |
| `stats` | Returns [telemetry](api.md#telemetry-and-health) from `GET /api/stats`. May poll the monitored inference server; cached/log-driven data is not proof of readiness. |
| `doctor` | Returns the [diagnostic report](api.md#diagnostics). Runs bounded device/NVIDIA probes without loading a model; a failed check gives exit 1 and retains the report in `data`. |
| `configs list` | Returns `{configs: [...]}` with saved configurations. |
| `configs show NAME` | Returns one configuration matched by its exact name; missing names fail with exit 1. |
| `launch (--config NAME \| --config-file PATH) [--wait] [--timeout SECONDS]` | Exactly one configuration source is required. Launches an existing saved configuration or unsaved JSON; `PATH=-` reads stdin. Replaces the currently managed model and returns launcher state. `--wait` adds a `readiness` result pinned to this launch. Wait timeout defaults to 300 seconds. Does not save the supplied configuration. |
| `restart [--wait] [--timeout SECONDS]` | Restarts the manager's current launch configuration, including unsaved settings; fails if none is available or switching blocks it. Returns launcher state; optional readiness behaves like launch. Timeout defaults to 300 seconds. |
| `stop` | Stops the managed primary server and cancels its pending restoration, if any. Returns launcher state. Does not stop an arbitrary external watch target. |
| `wait (--backend \| --ready) [--timeout SECONDS]` | Exactly one condition is required. Backend mode returns health; ready mode returns readiness for the first observed managed run. Timeout defaults to 300 seconds. |
| `smoke [--timeout SECONDS]` | Sends one short chat completion through the managed primary model. Returns `run_id`, `elapsed_seconds`, `message`, `finish_reason`, and `usage`. Default 60 seconds; positive finite maximum 300. Consumes inference resources and may create history. |
| `logs [--tail N]` | Returns a snapshot of the last N complete lines in the console API's bounded initial window; default 100, positive integer. Includes console metadata. Text mode prints only content. Does not follow logs indefinitely. A final unterminated startup line can be included after a crash. |
| `history list [FILTERS]` | Returns a [history page](api.md#history-and-prompts), including `items`, `next_cursor`, `models`, `database_path`, and `log_status`. Filters are listed below. |
| `gaming status` | Returns [gaming state](api.md#gaming-integration); it does not enable, install, or alter the integration. |
| `startup status` | Returns [Windows startup state](api.md#windows-startup); it does not install or remove a scheduled task. |

There are no CLI commands to create/delete saved configurations, clear history,
read individual saved prompts, change launcher settings, or mutate gaming/startup
settings. Use the dashboard or the corresponding HTTP API for those operations.

### History filters

| Option | Default | Meaning |
| --- | --- | --- |
| `--model NAME` | All models | Exact model/launch alias match, not necessarily the saved configuration name. |
| `--state STATE` | All states | Exact stored state: `running`, `complete`, `incomplete`, `ambiguous`, `error`, or `prompt_only`. |
| `--from-ts NUMBER` | No lower bound | Inclusive observation time, Unix seconds. |
| `--to-ts NUMBER` | No upper bound | Exclusive observation time, Unix seconds. |
| `--sort KEY` | `time` on backend | `time`, `duration`, `prompt_tokens`, or `generated_tokens`. |
| `--order ORDER` | `desc` on backend | `asc` or `desc`. |
| `--limit N` | `50` | Integer; the backend clamps it to 1–200. |
| `--cursor STRING` | First page | Opaque `next_cursor` from the previous page; keep the same filters, sort, and order. |

Managed history is collected independently of dashboard/stats requests. Its model
identity uses the launch alias, otherwise the GGUF filename. Ingestion is
asynchronous, so a just-finished request may not appear immediately.

## Configuration JSON

Use this configuration object for `launch --config-file`, HTTP launch, or saving
a configuration through the API. This is one configuration, not the entire
`state.json` file or a `{configs: [...]}` wrapper:

```json
{
  "name": "My model",
  "model_path": "/models/model.gguf",
  "port": 8001,
  "log_prompts": false,
  "flags": [
    {"flag": "--ctx-size", "value": "4096"},
    {"flag": "--alias", "value": "my-model"},
    {"flag": "--mlock", "value": "", "enabled": false}
  ]
}
```

| Field | Meaning |
| --- | --- |
| `name` | String saved configuration identifier; nonblank name required when saving. The launch alias/filename identifies history independently. |
| `model_path` | String path to an existing `.gguf` on the backend filesystem. Docker uses `/models/...`; native uses a path accessible to the backend account. Required to launch. |
| `port` | Required launch port, integer 1–65535. Docker requires internal port 8001; change the host mapping through `INFERENCE_PORT`. |
| `log_prompts` | Optional boolean, defaults off. Requests prompt capture via the binary's `--log-prompts-dir` capability; launch fails if requested but unsupported. |
| `flags` | Optional list of flag objects; absent/empty means no user flags. Each string `flag` is an argument and nonblank string `value` is its next argument. Use an empty value for a switch. |
| `flags[].enabled` | Optional; omitted/true includes the flag, false retains it in the configuration but skips it at launch. |

Configure the executable separately in **Manage** or through
[`POST /api/launcher/settings`](api.md#configurations-and-filesystem-browsing).
Do not put a binary path in this JSON. The launcher injects its managed port,
metrics, and log-file arguments, plus supported logging options. Flag values are
individual arguments, not shell command strings; do not add shell quoting around
paths inside JSON strings. Windows backslashes must be JSON-escaped, or use
forward slashes.

## Examples

### Launch, verify, inspect, and stop

Use a backend whose managed model you intend to replace. Save a configuration
named `My model` through the dashboard first:

```sh
python cli.py wait --backend --timeout 30 --json
python cli.py status --json
python cli.py configs show "My model" --json
python cli.py launch --config "My model" --wait --timeout 300 --json
python cli.py smoke --timeout 60 --json
python cli.py history list --model my-model --limit 10 --json
python cli.py stats --json
python cli.py logs --tail 100 --json
python cli.py restart --wait --json
python cli.py stop --json
```

Check each exit code before continuing: in PowerShell use `$LASTEXITCODE`, in a
POSIX shell use `$?`. For an existing launch, use `wait --ready --json` separately.
Use `doctor --json` and `logs --json` to investigate failures before retrying.

### Unsaved file or stdin

Save the configuration object above as `config.json`. Native:

```sh
python cli.py launch --config-file config.json --wait --json
```

The CLI reads this file on the machine/container where it runs, while model paths
are resolved on the backend. Pass a host file into Docker over stdin:

```sh
# POSIX shell:
docker compose exec -T monitor python cli.py launch --config-file - --wait --json < config.json
```

```powershell
# PowerShell:
Get-Content -Raw ./config.json | docker compose exec -T monitor python cli.py launch --config-file - --wait --json
```

### Pagination in PowerShell

```powershell
$page = python cli.py history list --model my-model --limit 10 --json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw $page.error.message }
$page.data.items
if ($page.data.next_cursor) {
    $next = python cli.py history list --model my-model --limit 10 --cursor $page.data.next_cursor --json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw $next.error.message }
    $next.data.items
}
```

## Output and exit codes

JSON success contains `ok` and `data`. Handled errors contain `ok: false` and an
`error` with a message and numeric exit code. For example, `wait --backend --json`:

```json
{"ok": true, "data": {"status": "ok"}}
```

Readiness with `state: "stopped"` can be returned successfully by HTTP or inside
`status`; `wait --ready` instead treats that condition as a CLI failure:

```json
{"ok": false, "error": {"message": "No managed server is running.", "code": 1}}
```

| Exit | Meaning |
| --- | --- |
| `0` | Command succeeded; interpret the returned state for backend-only commands. |
| `1` | Backend operation/verification failed, configuration name not found, logs unavailable, or diagnostics unhealthy. Non-504 HTTP error responses also map here, including backend validation errors. |
| `2` | Invalid CLI arguments, URL, unreadable/malformed configuration file, or configuration JSON not an object. |
| `3` | Connection failure, invalid JSON/protocol response, or unexpected response structure. |
| `4` | Request/wait/probe timeout or HTTP 504. |
| `130` | Interrupted. |

On diagnostic failure, `doctor` returns **both** `data` (the full report) and
`error`, with `ok: false` and exit 1. Without `--json`, successful results print
formatted `data` JSON; logs print plain content. Errors print to stderr, except
`doctor` prints its failure report to stdout. Help always prints ordinary text.

## Timeouts and verification

- **Backend available:** `/api/health` answers. No model is required.
- **Process started:** launch/restart returns after startup checks; it does not
  establish that the model has finished loading.
- **Model ready:** the same managed run owns its inference listener and its
  fresh health request succeeds. Gaming/recovery transitions block verification.
- **Inference verified:** `smoke` accepts nonempty answer or reasoning text from
  one nonstreaming chat request with a 16-token output limit. It does not assert
  an exact answer, performance, an embedding endpoint, or an external watch target.

Waits poll about every 0.5 seconds under a deadline. `--timeout` on launch/restart
applies only with `--wait`, to the wait after the launch HTTP call, not the whole
command. Each wait request is capped by the remaining deadline and
`--request-timeout`. Ready waits fail if the run changes, exits, or is blocked.
Only `wait --backend` retries transport connection/timeout failures; protocol
errors are not retried. Mutations and inference are never automatically retried.

Smoke uses its own overall deadline; its HTTP timeout is that value plus two
seconds, rather than `--request-timeout`. A timed-out submitted operation may
still finish. Inspect state before resubmitting it.
