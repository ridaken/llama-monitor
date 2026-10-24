# HTTP API reference

This is the llama-monitor **control and monitoring API**, normally at
`http://127.0.0.1:8500`. llama-server's OpenAI-compatible inference API is a
separate service, normally port 8001 for managed launches (`/v1/...`). The monitor
does not proxy that API. Docker host ports can be changed in `.env`.

- [Conventions and access](#conventions-and-access)
- [Telemetry and health](#telemetry-and-health)
- [Launcher and verification](#launcher-and-verification)
- [Configurations and filesystem browsing](#configurations-and-filesystem-browsing)
- [History and prompts](#history-and-prompts)
- [Gaming integration](#gaming-integration)
- [Windows startup](#windows-startup)

See the [README](../README.md) for installation and feature explanations and the
[CLI reference](cli.md) for a noninteractive client, including the canonical
[configuration JSON](cli.md#configuration-json).

## Conventions and access

Send JSON bodies with `Content-Type: application/json`. Responses below are raw
JSON objects, **without** the CLI's `{ok, data}` envelope. Unless a route says
otherwise, success is HTTP 200. Handled operation failures generally return
`{"error": "message"}` with the status listed for that route. FastAPI parameter
or typed-body validation returns HTTP 422 with `detail`; clients must not assume
one uniform error schema. Flexible dictionary bodies are not fully validated
schemas: send the documented field types. Unexpected failures can return HTTP 500.

Times such as `started_at`, `observed_at`, and `completed_at` are Unix seconds;
durations are seconds. Memory fields are bytes, power is watts, temperatures are
degrees Celsius, utilization/`percent` fields are percentages, and ratio fields
are fractions. Missing or null values mean unavailable, not measured zero.
Encode path parameters and query values, particularly names, paths, and cursors.

The dashboard has no built-in authentication. Anyone who can reach ordinary
launcher, configuration, history, or browsing routes can use them. Native binds
to loopback by default; Compose publishes on all interfaces by default. Follow
the [trusted-network guidance](../README.md#install-with-docker-compose-recommended).
Filesystem paths refer to the **backend** account/container, not the client PC.

Gaming and startup mutations have additional local checks: the caller must be
loopback and any supplied `Origin` must match the backend base URL. They are
disabled in Docker and test mode and target native Windows integrations. The two
Apollo hook routes also require `X-Llama-Monitor-Token`, provisioned by the local
integration setup. These checks do not authenticate the other dashboard routes.

FastAPI also exposes `/docs`, `/redoc`, and `/openapi.json`. They describe route
discovery and typed parameters, but many payloads use dictionaries/JSONResponse;
use this reference for those field contracts.

## Telemetry and health

| Method and route | Inputs | Response and effects |
| --- | --- | --- |
| `GET /api/health` | None | `{"status":"ok"}`. Backend-only probe; no inference readiness check. |
| `GET /api/stats` | Query `lite` integer, default `0` | Telemetry object described below. May contact the monitored server. Nonzero `lite` reduces idle HTTP collection when log gating is unavailable; it does not disable hardware collection. |

Stats is best-effort telemetry. An offline model or failed NVML collection is
represented in the body, normally with HTTP 200. Log-driven idle results can
reuse cached model data (`stale: true`) after the initial bootstrap scrape;
`online` is not a fresh readiness guarantee. Hardware remains collected during
gaming while stale model/inference fields are removed.

| Field | Contents |
| --- | --- |
| `online`, `active`, `log_mode`, `metrics_enabled`, optional `stale` | Monitored server/activity/collection indicators. |
| `model` | `name`, optional `path`, `n_ctx`, `total_slots`; `/v1/models` fallback may provide only a name. |
| `slots` | `busy`, `total`, `list` of `{id, state, prompt_tokens, prompt_processed, prefill_ratio, decoded, ctx_used, ctx_ratio}`. Slot state is `idle`, `prefill`, or `generating`. |
| `requests` | `processing`, `deferred` from metrics. |
| `kv` | `usage_ratio`, `tokens`; prefers server metrics, otherwise estimates occupancy from prompt/generated tokens held across slots, including retained prompts. |
| `throughput` | `pp_tps_avg`, `decode_tps_avg` from scrape gauges, and `decode_tps_live` from changes in active slot counters. |
| `spec` | `enabled`, `tokens_per_decode`; when logged, also `mean_len`, `accept_rate`, `accepted`, `generated`. Live efficiency is normalized per busy sequence; log values describe the last complete observed generation. |
| `prefill_last` | Latest available prompt timing `{tokens, secs, tps}`. |
| `last_request` | `pp`, `generation`, `total` timing objects from the newest complete generation in the current run. Generation TPS follows the server convention of excluding the first generated token from the rate numerator. |
| `log_status` | `available`, `configured`, `gap`, `error` for log coverage. |
| `gpu` | `ok`, optional `error`, `devices`. Each device can include `index`, `name`, `uuid`, `pci_bus_id`, `mem_total`, `mem_used`, `llama_mem`, `temp`, `util_gpu`, `util_mem`, `power`, `power_limit`, `error`. Individual readings may be missing. |
| `sysmem` | `ok`, optional `error`; when available, `total`, `used`, `available`, `percent`, `llama_rss`. RSS sums discovered llama processes; it is not a model-weight allocation measure. Docker reports RAM visible to its Linux environment. |
| `split`, `split_source`, `split_log_configured` | Memory entries `{label, bytes, kind, gpu_index?}`. Source is `nvml`, `log`, `nvml-delta`, or null. Logged buffer sizes are allocations, not residency; deltas are estimates affected by other processes. Docker avoids matching host NVML PIDs to container PIDs. |
| `layer_placement` | `status`, `reason`, `devices`; complete maps can add `total_layers`, `output_device`. Device entries have `label`, `layers`, optional `name`, `identity`, `gpu_index`. Status: `available`, `shared`, `loading`, `no_model`, `unavailable`. Main-model assignments exclude draft/projector layers and provisional memory-fit trials. |
| `runtime` | `{containerized, models_dir, managed_port}`. Container values are true, `/models`, 8001; native values are false, null, null. |
| `gaming` | Public switching state described below; no credentials or captured raw invocations. |

## Launcher and verification

### Launcher state and lifecycle

| Method and route | Inputs | Response, failures, and effects |
| --- | --- | --- |
| `GET /api/launcher/state` | None | Launcher state below; refreshes process status. Does not prove readiness. |
| `POST /api/launcher/launch` | Configuration object | Launcher state. Validates the executable, existing GGUF path, and port; stops/replaces the previous managed server and retargets monitoring. Does not save a named configuration. HTTP 400 for handled launch errors, including pending switching/recovery. |
| `POST /api/launcher/restart` | No body required | Launcher state; restarts the manager's current configuration. HTTP 400 if none exists or restart is blocked/invalid. |
| `POST /api/launcher/stop` | No body required | Launcher state; stops the managed primary and cancels its pending restoration. HTTP 409 for handled stop failures. Does not stop an arbitrary watched server. |
| `GET /api/launcher/flags` | None | `{source, flags}`: source `help` or `bundled`; entries `{flags: [aliases], value_hint, desc}`. Runs the configured binary's `--help`, cached per path/mtime; falls back on absence, failure, or empty parsing. |

Launch uses `model_path`, `port`, optional `flags` and `log_prompts`, plus `name`
for the configuration label. See [configuration JSON](cli.md#configuration-json).
The binary comes from launcher settings. Docker fixes the internal port to 8001
and adds `--host 0.0.0.0`. Managed port/metrics/log-file arguments are appended by
the launcher; supported structured logging and capability-checked verbosity 5
are added as explained in the README. Launch success establishes process startup,
not a loaded model. Native processes survive backend shutdown and may be adopted
after verifying their identity; container/test-owned processes stop with the backend.

Launcher state fields:

| Field | Contents |
| --- | --- |
| `settings` | `llama_server_path`, `models_dir`, `default_port`, `default_config`. |
| `binary_valid` | Whether the configured/default binary resolves. |
| `configs` | Saved configuration objects. |
| `status` | `state` (`running`, `stopped`, `exited`), `config_name`, `config`, `pid`, `exit_code`, `started_at`, `last_error`, `adopted`, `run_id`. Nullable fields identify missing process/configuration data. Pending recovery can expose its primary configuration. |
| `managed_log` | Backend managed log path. |
| `gaming`, `runtime`, `test_mode` | Public switching state, deployment information, and isolated-test indicator. |

### Readiness and smoke

| Method and route | Inputs | Response, failures, and effects |
| --- | --- | --- |
| `GET /api/launcher/readiness` | None | `{state, ready, run_id, pid, reason}`. States: `stopped`, `loading`, `ready`, `failed`, `blocked`. Normally HTTP 200 even when not ready. Verifies managed identity, listener ownership, and a fresh inference `/health`; switching/recovery blocks it. |
| `POST /api/launcher/smoke` | JSON object, e.g. `{}`; optional `timeout`, positive finite seconds ≤300, default 60 | `{run_id, elapsed_seconds, message, finish_reason, usage}` after one managed chat completion. HTTP 409 for verification/inference failure, 504 for deadline, 422 for invalid/missing typed body. Checks that the managed run remains unchanged. |

Readiness is for the managed primary, not an external watch target. Example:

```sh
curl http://127.0.0.1:8500/api/launcher/readiness
```

```json
{"state":"stopped","ready":false,"run_id":null,"pid":null,"reason":"No managed server is running."}
```

Smoke checks readiness first; it does not wait for a loading model to become
ready. It sends `Reply with OK.` with temperature 0, `max_tokens: 16`, and
`stream: false` to the managed `/v1/chat/completions`. Nonempty answer or reasoning
text succeeds; no exact answer is required. It consumes inference resources and
can add history. It does not verify embedding-only servers. A timed-out inference
request may still finish; inspect state before repeating an operation.

### Diagnostics

`GET /api/launcher/doctor` accepts no inputs and returns
`{healthy, summary, containerized, checks, recovery}`. Each check has `name`
(`server_devices` or `nvidia`), `state` (`pass`, `fail`, `skipped`), `exit_code`,
`message`, `output`, and optional `truncated`. `recovery` is a list of guidance
strings. **HTTP 200 includes unhealthy reports**; inspect `healthy` and checks.

The endpoint runs the selected binary's `--list-devices` and available
`nvidia-smi`, each bounded to four seconds and the last 16 KiB of output. Missing
NVIDIA tooling is skipped; a missing binary fails. It does not load/stop models,
change settings, or restart Docker. Passing diagnostics is not proof of inference
or model compatibility. CLI `doctor` converts unhealthy reports to exit 1.

### Console

`GET /api/launcher/console` accepts query `offset` (integer byte position, default
0) and `source` (previous response's source, default empty). It returns
`{available, content, offset, size, path, source?}`, optionally `error`. File
absence/read failure normally uses HTTP 200 with `available: false`.

It reads the monitored managed/external log, falling back to captured startup
output while the managed log is empty. Sources are `managed`, `external`, and
`startup`. Begin at offset 0; pass the returned offset and source for subsequent
incremental reads. A changed source or invalid/out-of-range offset resets the
read. Initial reads are bounded to roughly the last 256 KiB and discard a partial
first line. Subsequent reads return complete lines and a byte offset; a final
unterminated startup line is included after process exit. Text/JSONL logs are
decoded to readable messages. This is a snapshot endpoint, not an SSE stream.

## Configurations and filesystem browsing

| Method and route | Inputs | Response, failures, and effects |
| --- | --- | --- |
| `GET /api/configs` | None | `{configs: [...]}`. |
| `POST /api/configs` | One configuration object | `{configs: [...]}` after replacing/adding by name. Nonblank `name` required (HTTP 400 otherwise). Saving is separate from launch validation: it does not check model/binary compatibility or start a process. |
| `DELETE /api/configs/{name}` | Encoded configuration name | `{configs: [...]}` after deletion; also clears a matching default. Missing name is a successful no-op. Does not stop a running model. |
| `POST /api/configs/default` | `{name: "saved name"}`; absent/null/empty name clears | Full launcher state. HTTP 400 for unknown nonempty name. Marks the form's default when no managed server runs; does not launch it. |
| `POST /api/launcher/settings` | Optional `llama_server_path`, `models_dir`, `default_port` | Full launcher state after persisting non-null supplied values. Changes preferences/binary selection; does not restart the current model. |
| `GET /api/browse` | Query `path` default empty, `ext` default empty | `{path, parent, dirs, files}` with backend paths. Empty path lists Windows drives or opens the account's home directory elsewhere. `ext` is comma-separated extensions including dots, e.g. `.gguf,.exe`, case-insensitive; directories are always listed. HTTP 400 on directory-read failure. |

`default_port` initially equals 8001. `models_dir` initially is `/models` in Docker,
otherwise null. The executable is initially discovered from `PATH`. No CLI
command mutates these settings or saves configurations.

Example for a native Windows backend, in a POSIX shell using `curl`:

```sh
curl -X POST http://127.0.0.1:8500/api/launcher/settings \
  -H 'Content-Type: application/json' \
  -d '{"llama_server_path":"C:/llama.cpp/llama-server.exe","default_port":8001}'
```

To save a configuration, put the [canonical configuration object](cli.md#configuration-json)
in `config.json` and submit it (this does not launch it):

```sh
curl -X POST http://127.0.0.1:8500/api/configs \
  -H 'Content-Type: application/json' --data-binary @config.json
```

## History and prompts

| Method and route | Inputs | Response, failures, and effects |
| --- | --- | --- |
| `GET /api/history` | Query filters below | `{items, next_cursor, models, database_path, log_status}`. HTTP 400 for unsupported sort/state/order or invalid cursor; 422 for invalid typed query values. |
| `GET /api/history/{activity_id}/prompt` | Encoded history item ID, including `prompt:` IDs | `{prompt_text, truncated}`; HTTP 404 if unavailable. Returns captured rendered prompt text, not the full HTTP request or response. |
| `DELETE /api/history` | None | `{deleted: N}` counts removed generation and unlinked prompt records. Clears stored prompt text and managed prompt files, retaining log cursors so old lines do not reappear. External prompt files and raw logs are not deleted. There is no API confirmation step. |

History query parameters:

| Parameter | Default | Meaning |
| --- | --- | --- |
| `model` | Empty/all | Exact alias/model name. |
| `state` | Empty/all | `running`, `complete`, `incomplete`, `ambiguous`, `error`, `prompt_only`. |
| `from_ts`, `to_ts` | Unset | Unix observation seconds, inclusive lower bound and exclusive upper bound. |
| `sort` | `time` | `time`, `duration`, `prompt_tokens`, `generated_tokens`. |
| `order` | `desc` | `asc` or `desc`; tied values are ordered by item ID. |
| `limit` | `50` | Integer clamped to 1–200. |
| `cursor` | Unset | Opaque prior `next_cursor`; keep filters and ordering unchanged when paging. |

Items contain `id`, `run_id`, nullable `slot_id`/`task_id`, `model`, `observed_at`,
`completed_at`, `state`, `prompt_tokens`, `generated_tokens`, `prompt_seconds`,
`decode_seconds`, `total_seconds`, `draft_accepted`, `draft_generated`,
`draft_accept_rate`, `draft_mean_len`, `has_prompt`, and `prompt_truncated`.
Unknown values are null. Prompt-only items lack slot/task/timing information.
Missing sort values sort as -1. `models` lists available stored model identities;
`next_cursor: null` ends pagination. `log_status` includes `available`,
`configured`, `gap`, `error`, `prompts_configured`.

Example first page with no matching entries:

```sh
curl 'http://127.0.0.1:8500/api/history?model=my-model&limit=10&sort=time&order=desc'
```

```json
{"items":[],"next_cursor":null,"models":[],"database_path":"/data/history.sqlite","log_status":{"available":false,"configured":false,"gap":false,"error":null,"prompts_configured":false}}
```

History is ingested asynchronously from logs, independent of browser polling;
it is not a complete HTTP request audit. Records have no automatic expiry.
Overlapping prompts without a confident timing match remain `prompt_only` rather
than being assigned to another request. See the [history explanation](../README.md#activity-history)
for prompt privacy and retention.

## Gaming integration

All mutation routes below require the local access checks described above;
hook routes additionally require the integration token. Setup requires the
[supported Apollo integration build and installer](../README.md#automatic-moonlight-gaming-mode-native-windows--apollo).
Passwords/hook tokens use Windows user-bound DPAPI and are never returned.

### State

`GET /api/gaming/state` accepts no inputs and returns public gaming state plus
`commands: {prepare, "session-ended"}` containing hook command strings.
Launcher/stats and most mutation results also include gaming state; only routes
that explicitly return the GET handler add `commands`.

| Field | Contents |
| --- | --- |
| `supported`, `enabled` | Native Windows capability and saved enable setting. Test mode separately disables mutations. |
| `phase`, `reason`, `blocked` | Phase `normal`, `stopping`, `gaming`, `countdown`, `restoring`, `retry_countdown`, or `failed`. Reason `gaming`, `startup`, or null; blocked means phase is not normal. |
| `connected_clients`, `countdown`, `integration_error` | Client count (null if unknown), seconds remaining (or null), current reconciliation error. Unknown Apollo state holds restoration. |
| `servers` | Captured transition entries `{id, name, port, status, attempts, error}`. IDs `primary`/`embeddings`; status can be `running`, `stopped`, `stop_failed`, `loading`, `ready`, `failed`, `cancelled`. Empty outside a transition. |
| `apollo_url`, `credentials_saved`, `certificate_sha256` | Connection/pinning metadata; credentials themselves are excluded. |
| `auxiliary` | Registered `{executable, port}` or null; raw process arguments/snapshots are excluded. |

### Controls and hooks

| Method and route | Inputs | Response, failures, and effects |
| --- | --- | --- |
| `POST /api/gaming/settings` | JSON object; optional `apollo_url`, `username`, `password`, `enabled` | State with commands. Default URL `https://localhost:47990`; URL must be local HTTPS. A nonblank password requires a valid username and replaces credentials; blank preserves them. Connection/credential changes clear certificate trust. Enable requires saved credentials, pinned/tested Apollo, and no explicit watch target. Persists hook configuration/token. HTTP 400 for handled failures, including active transitions or access rejection. |
| `POST /api/gaming/test` | No body required | State with commands after testing the local Apollo connection and saving its certificate fingerprint. Explicitly establishes/replaces local certificate trust; not a read-only check. HTTP 400 for handled failures. |
| `POST /api/gaming/auxiliary` | JSON object, optional `port` default 8081 | State with commands after registering a running llama-server matching the configured executable and selected port. Port must be 1–65535 and differ from the primary; no matching process is an error. Does not launch a server. HTTP 400 for handled failures. |
| `POST /api/gaming/prepare` | Token header, no body required | Gaming state after blocking until captured process exits are confirmed. Disabled integration returns `{enabled:false, skipped:true}`. Persists restoration intent before stopping servers; HTTP 409 for handled failure/access/token rejection, retaining ownership on shutdown failure. |
| `POST /api/gaming/session-ended` | Token header, no body required | HTTP 202 `{scheduled:true}`; wakes reconciliation. This notification does not prove that all streams ended. HTTP 409 for handled rejection. |
| `POST /api/gaming/retry` | No body required | Gaming state after retrying a failed restoration. Requires confirmed zero clients and a reconciled failed transition without shutdown failures. HTTP 409 for handled failure/access rejection. |
| `POST /api/gaming/cancel` | No body required | Gaming state after cancelling pending restoration intent, without starting/killing processes. HTTP 409 for handled failure/access rejection. |
| `POST /api/gaming/servers/{server_id}/stop` | `server_id`: `primary` or `embeddings`; no body required | Gaming state after stopping/cancelling the selected transition server. Primary also stops the manager; embedding handling applies to owned transition processes. HTTP 409 for handled failure, unknown ID, or access rejection. |

The backend reconciles Apollo independently of browser tabs. After the last
client disconnects it normally waits 60 seconds, then restores only captured
servers; each failed load gets one automatic retry after 60 seconds. Launch and
restart are blocked while switching/recovery is pending. See the README for
installation, reservation timing, cancellation, and rollback details.

## Windows startup

| Method and route | Inputs | Response, failures, and effects |
| --- | --- | --- |
| `GET /api/startup/state` | None | Startup state below; also consumes a completed native setup result, if pending. |
| `POST /api/startup/install` | JSON object, optional `mode` (`boot` default, or `logon`), `autostart_models` (boolean, default true) | HTTP 202 startup state after opening native elevated setup. Captures running model invocations if requested; errors if none can be captured. Requires a non-explicit watch target and local native Windows access. HTTP 400 for handled validation/access/setup failure. |
| `POST /api/startup/remove` | No body required | HTTP 202 startup state after opening native removal setup. Removes the app's scheduled task and disables boot model loading when completed; does not stop running models or remove Apollo hooks. HTTP 400 for handled failures. |

Startup state contains `supported`, `installed`, `mode`, `autostart_models`,
`models` (public `{id, name, port}` entries), `pending` (boolean), `last_error`,
`last_result`, `task_name`, and `log_path`. Support is false outside native Windows
and in test mode. No password or raw captured arguments are returned.

HTTP 202 means setup was started, not installed successfully. Poll state until
`pending` clears and inspect `installed`/`last_error`/`last_result`. Installation
and removal refuse an already pending operation or active model transition.
Windows presents elevation and, for boot mode, its native credential dialog;
never submit a Windows password to this API. The task runs the backend hidden
under the user's account. Installation applies at the next boot/sign-in and does
not restart the current backend. Model loading is once per boot and yields to
existing servers and gaming recovery; see [Windows startup](../README.md#automatic-windows-startup-native-installation).
