# llama-monitor

A local web dashboard for a `llama.cpp` / `llama-server` session, with a React and
shadcn/ui interface. The **Monitor**, **Manage**, and **History** views keep live
status visible while you move between them.
It can **launch and manage** llama-server for you (browse to a `.gguf`, set
flags, Launch / Stop / Restart, and save/load named configurations), and shows,
live (about 1s while active, 3s while idle, plus request time):

- **Model loaded** — name, context size, slot usage, KV-cache usage as
  `tokens / n_ctx (percent)`
- **Throughput** — prompt (pp) and decode tokens/sec, with a 60-sample smoothed decode
  sparkline (with Y-axis), speculative-decode efficiency, and last-prefill timing
- **Slots** — per-slot state (idle / prefill / generating), context fill, prompt
  length and tokens generated
- **Hardware** — a CPU & RAM card and individual GPU cards, with primary model
  layer assignments and separate output-layer placement. Includes RAM usage and
  trend, llama-server RSS, per-GPU temp/util/power trends, VRAM, and total GPU draw.
  Teal marks llama-server memory; when NVML measures its VRAM, the capacity bar
  shows teal for llama-server and orange for other usage. Logged weights,
  context and work buffers are a separate labeled row, since they do not measure
  live residency. CPU buffers include mapped files and pinned RAM. Devices
  without a confirmed hardware identity retain their own labeled card.
  Docker RAM describes the container/VM, not Windows host RAM.
- **Activity history** — sortable, filterable generations observed in the log,
  retained in local SQLite until you clear them

Designed to run on the **same machine** as llama-server and the GPUs.

## Contents

- [How it works](#how-it-works)
- [Docker installation](#install-with-docker-compose-recommended)
- [Native Python installation](#native-python-installation-alternative)
- [Launch and manage](#launch--manage-llama-server)
- [Hardware memory and layer placement](#hardware-memory-and-layer-placement)
- [Activity history](#activity-history)
- [Moonlight gaming mode](#automatic-moonlight-gaming-mode-native-windows--apollo)
- [Windows startup](#automatic-windows-startup-native-installation)
- [Data sources](#how-it-gets-the-data) and [adaptive polling](#polling-log-driven-when-idle-no-console-spam)
- [CLI quick start](#command-line-verification), [full CLI reference](docs/cli.md), and [HTTP API reference](docs/api.md)
- [Startup troubleshooting](#model-startup-troubleshooting)
- [Development and tests](#development)

## How it works

The browser and [CLI](docs/cli.md) talk to the same Python/FastAPI backend. It
starts one managed primary llama-server, or watches a server you started yourself.
Model/slot data comes from llama-server HTTP endpoints; a background log reader
collects startup allocations and generation timings and stores activity in local
SQLite. NVML supplies NVIDIA GPU telemetry, and psutil supplies RAM/process data.
There is no cloud account or model download step.

**Monitor** combines model controls, saved configurations, and telemetry.
**Manage** holds executable selection, proxy controls, and native Windows integrations.
**Requests** explores captured proxy request/response pairs.
**History** explores saved generations and optional native prompts. Log collection and
Windows coordination run without an open browser; live chart samples are kept in
the browser and restart on reload. The browser retains up to 60 samples per trend,
with a three-sample average for decode speed, so the displayed time span varies
with activity, missing readings, and response time.

The monitor's [control API](docs/api.md) normally uses port 8500. Inference clients
send requests directly to llama-server's OpenAI-compatible `/v1` API, normally port
8001 for a managed model. Optionally enable the inference proxy on port 8501 to
capture request/response pairs while preserving direct inference access.

### Speculative decoding (MTP / draft)

If the server runs with speculative decoding (e.g. `--spec-type draft-mtp`), the
Throughput panel shows **accepted tokens per step, per sequence** (≈1 means
speculation isn't helping; up to `spec_draft_n_max + 1` is ideal). If speculation
isn't detected, the section shows a disabled notice instead. Live efficiency is
estimated from generated-token changes and decode calls, normalized by busy
sequences; the newest complete logged generation supplies exact last-request
acceptance/mean-length figures when available. This explains the displayed
efficiency; it does not enable speculation for the model.

## Install with Docker Compose (recommended)

Compose builds llama-monitor locally and runs the dashboard and its managed
llama-server together. Python, Node.js, and a separate llama-server installation
are not required on the host. NVIDIA acceleration is enabled by default.

**Prerequisites:** Git, Docker with the Compose plugin, and a directory containing
your `.gguf` models. On Windows, use Docker Desktop's **Linux containers / WSL 2
backend**, an updated WSL kernel, and an NVIDIA driver with WSL support; see
[Docker's GPU prerequisites](https://docs.docker.com/desktop/features/gpu/).
On Linux, configure the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
for Docker. No privileged container or Docker socket mount is needed.

```sh
git clone https://github.com/ridaken/llama-monitor.git
cd llama-monitor
cp .env.example .env
```

On PowerShell, use `Copy-Item .env.example .env` instead of `cp`. Edit `.env` and
set `MODELS_DIR` to an **existing** model directory. Windows paths should use
forward slashes, for example `MODELS_DIR="D:/AI Models"`; Linux might use
`MODELS_DIR=/srv/models`. Alternatively create a `models` directory in the checkout
and put your GGUF files there. Models are mounted read-only and are not downloaded
or copied into the image. On Linux, the directory and model files must be readable
by the container user (UID/GID 10001).

```sh
docker compose up --build -d
```

Open **http://localhost:8500**, select a model under **`/models`**, configure flags,
and click **Launch**. The Linux llama-server binary is already configured. Use
container paths such as `/models/qwen.gguf`, including paths in flags for draft
models or adapters. Windows `.exe` files and arbitrary host paths cannot be used
inside this Linux container.

The dashboard is also available at `http://<host-LAN-IP>:8500`, and the managed
server's OpenAI-compatible API at `http://<host-LAN-IP>:8001/v1`. Both ports are
published on all host interfaces by default. **Use a trusted LAN:** the dashboard
has no built-in authentication, and connected users can browse container files
and control the managed server. Do not expose these ports directly to the internet.
Set `BIND_ADDRESS=127.0.0.1` in `.env` to restrict access to this computer.

`DASHBOARD_PORT` and `INFERENCE_PORT` in `.env` change the **host** ports, including
when another native installation already uses 8500 or 8001. The internal ports
remain 8500 and 8001; the launcher fixes its port to 8001 so publishing keeps
working. Apply port or mount changes with `docker compose up -d`.

### CPU-only installation

Explicitly selecting the base file omits the automatically loaded NVIDIA override
and builds using the CPU llama.cpp server image:

```sh
docker compose -f compose.yaml up --build -d
```

Use the same `-f compose.yaml` selection for subsequent CPU-only commands. CPU
inference, logs, history, and model controls work without NVIDIA hardware; GPU
panels will report unavailable. AMD/ROCm and Apple Metal acceleration are not
included in this setup.

### Container lifecycle, updates, and saved data

- `docker compose logs -f --tail 100` shows backend output; the dashboard's
  **Console** shows managed llama-server output. Container output rotates at
  three 10 MB files; managed model logs retain their existing lifecycle.
- `docker compose stop` stops the dashboard and gracefully stops its model.
  `docker compose down` also removes the container and network. Closing a browser
  tab leaves the model running.
- `docker compose up -d` starts the dashboard again. Saved configurations and
  history remain. By default, models must be launched manually after every
  container restart. Enable **Manage → Model startup → Start the default model
  when llama-monitor starts** to launch your saved default automatically.
- `restart: unless-stopped` restarts the dashboard after failures and Docker
  restarts. Configure Docker to start with your operating system separately;
  model loading follows the **Model startup** setting above.
- To update, pull repository changes and run
  `docker compose build --pull` followed by `docker compose up -d`.
  The `LLAMA_CPP_IMAGE` build argument, also configurable in `.env`, selects an
  upstream server image tag or digest. Leave it unset for the standard CPU/CUDA
  selection. Rebuild after changing it. Images use the upstream
  [llama.cpp server distribution](https://github.com/ggml-org/llama.cpp/blob/master/docs/docker.md).

The named volume `<compose-project>_monitor-data`, mounted at `/data`, holds
`state.json`, `history.sqlite` (including WAL files), model logs, and saved prompt
files. Native `~/.llama-monitor` data is separate and is not automatically
migrated. Before backing up the volume, stop Compose so the database is idle.
For example, with an existing `backup` directory and the default project name:

```sh
docker compose stop
docker run --rm -v llama-monitor_monitor-data:/data:ro -v "./backup:/backup" busybox:1.37 tar czf /backup/llama-monitor-data.tar.gz -C /data .
docker compose up -d
```

Use `docker volume ls` to confirm the actual volume name if the checkout directory
or Compose project name differs. Restoring into an empty volume should preserve
the original ownership (UID/GID 10001). `docker compose down --volumes` permanently
deletes saved settings, history, prompts, and logs; it does not delete host models.

GPU fields depend on NVIDIA's available telemetry; WSL may omit utilization,
temperature, power, or per-process details. Model memory split uses the managed
server's logs rather than matching container process IDs against host NVML IDs.
For managed launches, the launcher defaults logging to verbosity 5 when supported
and not explicitly overridden; see [layer placement](#hardware-memory-and-layer-placement).
Lowering verbosity through launch flags can hide allocation details.
**Docker system memory** describes RAM visible to the Linux environment (the
Docker VM on Windows), not Windows host RAM. The reported process RSS belongs to
the managed container process. No host process namespace is required.

Apollo gaming integration and Windows Task Scheduler setup require the **native
Windows installation** described below. Their controls explain this limitation
when running in Docker. The Docker dashboard cannot manage Windows host processes.

## Command-line verification

The CLI is included in the Docker image. It connects to the running backend and
uses the same controls as the dashboard; no host Python installation is needed:

```sh
docker compose exec -T monitor python cli.py status --json
docker compose exec -T monitor python cli.py doctor --json
docker compose exec -T monitor python cli.py configs list --json
docker compose exec -T monitor python cli.py launch --config "My model" --wait --timeout 300 --json
docker compose exec -T monitor python cli.py smoke --timeout 60 --json
docker compose exec -T monitor python cli.py stats --json
docker compose exec -T monitor python cli.py logs --tail 100 --json
docker compose exec -T monitor python cli.py history list --limit 10 --json
docker compose exec -T monitor python cli.py stop --json
```

For CPU-only installations, use `docker compose -f compose.yaml exec -T ...`.
Rebuild the image after source changes so the CLI and backend exercise that code.
Model paths in configurations must be backend paths, such as `/models/model.gguf`
in Docker. `launch --config-file PATH` reads an unsaved JSON configuration in the
existing configuration format; `--config-file -` reads JSON from stdin. The JSON
file is read where the CLI runs, so use stdin to pass a host file into Docker.

With a native backend, run `python cli.py ...` from the checkout and its Python
environment. `--url` overrides `LLAMA_MONITOR_URL`; the default is
`http://127.0.0.1:8500`. The CLI connects to an already running backend and never
prompts. Add `--json` for a machine-readable result and inspect the exit code.

`status` confirms backend availability even if no model runs. Launch/restart
confirm process startup; `--wait` verifies the same managed run owns its port and
passes a fresh health check. `smoke` sends one short chat completion and consumes
inference resources. It verifies the managed primary, not external watch targets
or embedding-only servers.

See the **[CLI reference](docs/cli.md)** for every command/option, configuration
JSON and stdin examples, history pagination, response envelopes, exit codes,
timeouts, and scripting limitations. The **[HTTP API reference](docs/api.md)**
covers all backend routes, including dashboard-only controls, response fields,
errors, and integration access requirements. This API is separate from the
managed server's inference API.

### Model startup troubleshooting

The **Run diagnostics** button in Console and `python cli.py doctor --json` run the same
read-only checks on the backend: `llama-server --list-devices` and, when installed,
`nvidia-smi`. Each subprocess has a four-second timeout and returns at most the
last 16 KiB of output. The dashboard runs these checks once when it observes a
server exit, and allows an explicit retry. They never load a model, change
settings, stop a server, or restart Docker. A passing report does not prove model
compatibility or inference; unavailable NVIDIA tools are reported as skipped.
The report is also available at `GET /api/launcher/doctor` (HTTP 200 even when
checks fail; inspect `healthy` and `checks`).

The launcher now captures stdout/stderr in `llama-server.log.startup` next to the
managed log. The file is reset on each launch and survives a backend restart.
Console and CLI logs use it when the managed log is empty, preserving errors
from failures before llama-server opens `--log-file`. Once that file has content,
Console uses it without duplicating stderr into history. Keep the startup file
for additional process output if the managed log has already opened.

On Linux, exit `-11` means the process received SIGSEGV; it does **not** identify
the underlying cause. In a real Windows Docker/WSL failure, even `--list-devices`
segfaulted and NVIDIA diagnostics failed while Windows still saw the GPUs.
A full Docker/WSL restart restored access without changing the model. If the
diagnostics show the same NVIDIA access failure, save work in **all containers
and WSL sessions** before running this from PowerShell on the Windows host:

```powershell
docker desktop stop
wsl --shutdown
docker desktop start
docker compose up -d --wait monitor
docker compose exec -T monitor python cli.py doctor --json
```

This interrupts other Docker containers and every running WSL distribution.
The app never performs it automatically. Inspect recovery of your other services,
then launch your saved model again. An ordinary Docker restart did not restore
GPU access in that incident; stopping WSL as well was necessary. Keep WSL,
Docker Desktop, and the Windows NVIDIA driver current. See
[Docker's WSL guidance](https://docs.docker.com/desktop/features/wsl/best-practices/)
and [GPU prerequisites](https://docs.docker.com/desktop/features/gpu/).

## Hardware memory and layer placement

Hardware reports **Assigned model layers: N / total** for the main model's
transformer layers. The output layer appears separately. Counts come from complete
startup-log assignments, not memory usage or requested `--gpu-layers` values.
They describe primary device assignment: expert/tensor overrides may store some
weights elsewhere. Known row/tensor splitting displays **Shared across devices**.
Main-model counts never include a separate draft model or projector.

For future managed launches, the launcher adds `--log-verbosity 5` when the
installed binary advertises it. Debug logging increases log volume. Explicit
`-lv`, `--verbosity`, `--log-verbosity`, verbose/log-disable flags and logging
environment overrides take precedence. For example, add `--log-verbosity 3` to
reduce logs; detailed placement may then be unavailable. User-provided
`LLAMA_ARG_LOG_VERBOSITY` is honored at any value; `LLAMA_ARG_LOG_DISABLE` and
`LLAMA_ARG_LOG_VERBOSE` also prevent automatic verbosity injection.

An already loaded or externally launched model needs retained debug startup records;
otherwise Hardware shows **Unavailable** with restart guidance. External launch
arguments are never modified. Hardware remains visible without a model, and counts
clear on managed stop, crash, or gaming suspension. GPU identities are matched by
PCI bus ID or UUID, not CUDA ordinal; unmatched logged devices get placement-only
cards rather than borrowing another GPU's telemetry. Extra device cards wrap and
scroll inside Hardware in desktop Fit mode; Comfortable uses natural page scrolling.

The [stats API reference](docs/api.md#telemetry-and-health) documents memory sources,
layer-placement states, and device identity fields. Use `python cli.py stats --json`
to inspect them from scripts. Zero layer counts require a complete transformer
layer map; placement adds no inference HTTP requests.

## Launch & manage llama-server

The **Monitor** view runs llama-server for
you, so you don't need a separate launch script. Model and launch controls sit
above Configuration, alongside live telemetry. The **Fit / Comfortable** control
beside the app navigation applies to Monitor, Manage, and History, and remembers
your choice across reloads. Fit arranges and compacts panels to keep their landing
content within the desktop viewport where practical. Comfortable uses more generous
spacing. Expanded Flags, additional details, or smaller windows can make the page
scroll in either mode. History records always scroll within **Explore records**,
with filters and pagination outside the records area. Manage retains executable,
Gaming, and startup settings:

- **Pick the binary** — the path to `llama-server` is auto-detected from your
  `PATH` on first run and shown in the panel. If it can't be found you get a
  warning linking to **Manage**, where Browse lets you pick the executable.
  Your choice is persisted across sessions.
- **Pick a model** — paste a path to a `.gguf` or **Browse** the filesystem.
- **Set flags** — add flags from the dropdown, parsed from your installed
  `llama-server --help` and alphabetised, or type any flag/value by hand. Help
  results are cached per binary build; a bundled common-flag list is used if the
  binary is missing, fails, or produces no parseable help. Each known flag shows a
  description next to it — whether picked from the dropdown or typed as a custom
  flag — and its value hint becomes the input placeholder. The Flags section
  also lets you disable a flag without deleting it from the saved configuration.
  It **collapses** (click the Flags label) to free up dashboard space; collapsed
  it shows a short preview and count — expand it to edit the complete list.
  Flags start collapsed on each dashboard load.
- **Console** — the **Console** button opens a live, auto-scrolling view of
  llama-server's console output (it tails the server's log file).
- **Port** — defaults to `8001`; change it if you like, but it can't be removed
  (the dashboard needs it to know where to monitor).
- **Launch / Stop / Restart** — llama-monitor starts the server and immediately
  **retargets its own monitoring** at it. A status pill shows
  `running` / `stopped` / `exited (code N)`.

llama-monitor **injects three flags** at known values so monitoring works:
`--port <your port>`, `--metrics`, and `--log-file` (pointed at
  `~/.llama-monitor/llama-server.log`, or `/data/llama-server.log` in Docker). When the installed binary advertises
`--log-jsonl` and `--log-timestamps`, it enables those too. Older binaries keep
using text logs. You don't set these yourself.

**Save / load configurations.** A saved config is the model path + your flags +
the port, stored under a name (defaults to the `-a`/`--alias` value, else the
`.gguf` filename — editable). Pick one from the **Configuration** dropdown to
load it. If you switch with unsaved edits, you're prompted to **Save**
(overwrite), **Save as new**, or **Discard**. Everything persists to
`~/.llama-monitor/state.json`.

**Default configuration.** Click **Set default** next to the Configuration dropdown to
mark the selected config as your default (it's flagged with a ★ in the list).
When you open the dashboard and **no server is running**, the default config is
loaded into the form automatically, ready to Launch. Click the ★ again to clear
it; deleting a config also clears it if it was the default.

**Automatic model startup.** In **Manage → Model startup**, check **Start the
default model when llama-monitor starts** to launch that saved configuration
once whenever the backend starts, even without a browser. This setting defaults
to off, works in native and Docker installations, and takes effect on the next
backend startup. Turning it off keeps the default form prefill. Changing the
checkbox does not start or stop a model immediately.

An existing managed model, an explicit `--llama-url` watch target, or pending
Windows/gaming recovery takes priority. Active Moonlight gaming or an unknown
gaming connection state skips that startup attempt. No default means nothing
launches. Skipped and failed launches are not retried; failures appear in Manage
and existing console/diagnostics remain available. Browser reloads, manual Stop,
and model crashes do not trigger another launch. Windows startup's captured-model
restoration remains separate and keeps its existing behavior.

> With the native installation, a launched server is left running when you close the dashboard — it's spawned
> detached, so killing/Ctrl+C-ing the dashboard (or closing its console) does
> **not** take the server down. Stop it explicitly from Monitor. Closing or
> reloading the tab prompts only when configuration edits are unsaved; the server
> keeps running either way.
> If you restart the dashboard while a launched server is still running, it
> **re-adopts** that server automatically (status, monitoring, and Stop/Restart
> all reconnect) — unless you pass an explicit `--llama-url` (see below), which
> means "watch exactly this" and takes precedence over re-adoption. Single
> instance: launching replaces any server the panel previously started.

## Automatic Moonlight gaming mode (native Windows / Apollo)

For the supported custom Apollo build, package installation, and program-file
rollback, see [Apollo build and recovery](docs/apollo-build.md).

The **Manage** view includes a Moonlight integration panel. An incoming streaming
connection stops the managed model and any registered embedding server, releasing
their model and KV-cache allocations. Active AI requests are interrupted. After
the last client disconnects, llama-monitor waits **60 seconds**, then restores
only the servers that were running before connection, using their captured
executable, arguments, working directory, models, ports, and unsaved launch settings.
It does not launch your default configuration instead.

If a load fails, the failed server gets one more attempt after **60 seconds**.
Successfully restored servers stay running. After the second failure, the
dashboard reports failure and stops retrying; **Retry failed models** explicitly
starts another recovery attempt. A game left running is not closed or suspended
and can prevent AI loading. Configured `--fit` behavior is preserved, so allocation
and context fitting can still vary with available memory. KV caches and interrupted
responses are not preserved. LAN AI clients must retry after the servers return.

### Install and validate

1. Restart the backend using **plain `python app.py`**, under the same Windows
   account Apollo uses for its unelevated application commands. Do not use an
   explicit watch target or set `LLAMA_URL` to a different URL. The backend must
   re-adopt the managed process. Windows startup is configured separately below.
2. In **Manage → Moonlight gaming integration**, enter Apollo's local HTTPS URL
   (normally `https://localhost:47990`), username, and password. **Save connection**,
   then **Test and trust local Apollo**. Credentials and the hook token use
   Windows user-bound DPAPI. The local TLS certificate is pinned before credentials
   are sent; a changed certificate requires another explicit test. Blank password
   input preserves the saved credentials.
   Install the supported [ridaken/Apollo integration build](https://github.com/ridaken/Apollo/tree/codex/llama-monitor-integration)
   first. It gives the browser and monitor independent authentication sessions.
   Before every JSON login (including renewal after HTTP 401), the monitor checks
   the pinned local `/api/configLocale` response for `auth_sessions: "multiple-v1"`.
   Stock or downgraded builds receive no login request, preventing browser logout.
   Polls reuse the monitor's in-memory cookie and renew it once on expiration or
   Apollo restart. Repeated failure holds restoration rather than assuming that
   clients disconnected. Credentials remain encrypted with Windows DPAPI.
3. Start your embedding server if necessary, then **Register running embedding
   server** on port **8081**. Registration matches the selected llama-server
   executable and port; it does not control every process named llama-server.
   It adopts the existing invocation without injecting monitoring flags. The
   primary model remains on its configured port (normally **8001**).
4. With no Moonlight streams active, run the installer from this repository using
   the same Python environment and Windows account, with write permission for
   Apollo's configuration directory:

   ```powershell
   python apollo_setup.py --install
   ```

   The installer backs up `sunshine.conf` and `apps.json` under
   `~/.llama-monitor/apollo-hook-backup.json`, adds a blocking global preparation
   command and an undo notification, and enables `terminate-on-pause` on **Desktop**
   and **Steam Big Picture**. Existing commands are retained. Restart Apollo.
   Installation leaves the integration disabled.
   Disabled hooks are harmless no-ops, so ordinary streaming remains available.
5. **Enable for connection testing**, then test Desktop and Steam Big Picture
   from a Moonlight client. Confirm both AI processes exit, streaming works, and
   the original endpoints return after disconnect + 60 seconds. Test reconnecting
   both during the countdown and after AI has reloaded; test two clients if used.
   Confirm a failed load receives only one automatic retry. Leave the integration
   enabled for normal use only after these tests pass.

Apollo 0.4.6's client and resume commands run asynchronously after stream startup.
The integration therefore uses blocking application preparation and ends the
streaming application session on final disconnect so reconnects run preparation
again. Desktop has no game process to terminate; Steam Big Picture's detached
games remain open, while its existing Big Picture cleanup runs. Apollo probes
encoders **before** preparation: verify this succeeds with AI loaded on your PC.
Automated tests do not establish that real GPU/encoder compatibility.

The helper at `scripts/apollo-hook.ps1` starts this backend hidden if it is absent,
without launching a model. It reads its user-bound token from
`~/.llama-monitor/apollo-hook.json`; secrets are not passed on command lines.
The backend reconciles Apollo's connected clients every two seconds independently
of browser tabs. A connection that never starts streaming gets a 30-second
reservation before the disconnect countdown begins. Unknown or unauthenticated
Apollo status holds restoration rather than treating it as zero clients.

**Launch / Restart** are blocked during switching. **Stop** cancels the primary's
pending restoration; each captured server also has **Stop / cancel restore**.
Recovery snapshots survive backend restarts. **Cancel automatic restoration**
forgets the pending retry intent without starting or killing a process.

### Roll back

Finish or cancel a pending transition, then run:

```powershell
python apollo_setup.py --rollback
```

Restart Apollo. Rollback disables the integration and restores the original
configuration bytes. If Apollo configuration was edited after installation,
automatic rollback refuses to overwrite those newer changes; use the backup to
remove the inserted global prep command and restore the two applications'
original pause settings manually. Do not remove other preparation commands.

For a nonstandard installation directory, pass `--config-dir` to `--install`.
The installer never stops Apollo, a game, or an AI process itself.

### Local integration API

`POST /api/gaming/prepare` blocks until all captured process exits are confirmed;
failed shutdown returns an error and retains ownership. Both that endpoint and
`POST /api/gaming/session-ended` require a loopback connection and
`X-Llama-Monitor-Token`. The latter returns `202` and schedules reconciliation;
it is not proof that all streams have ended. `GET /api/gaming/state` reports
phase, connected-client count, countdown, and per-server readiness and errors,
without raw invocation snapshots or secrets. Local dashboard controls also use
`POST /api/gaming/settings`, `/test`, `/auxiliary`, `/retry`, `/cancel`, and
`/servers/{primary|embeddings}/stop`. Launcher state and stats include the same
public switching status. During gaming GPU/system telemetry stays active while
stale model allocations and inference gauges are cleared.

## How it gets the data

| Data | Source |
|------|--------|
| Model / ctx / slots | llama-server `GET /props`, `/v1/models`, `/slots` |
| pp & decode TPS gauges, request counts | llama-server `GET /metrics` (needs `--metrics`) |
| Per-generation timings and history | text or JSONL server log, stored in SQLite |
| GPU temp/util/power/VRAM | NVML (`nvidia-ml-py`) |
| System RAM + llama-server RSS | `psutil` |
| Console output | the server's log file, tailed |
| Supported flags + descriptions | parsed from `llama-server --help` |
| GPU memory attribution | NVML per-process VRAM when available; otherwise logged buffers or a logged-baseline estimate |
| CPU buffer allocations | parsed from llama-server's startup log (optional), distinct from live RAM/RSS |
| Per-device model layers | complete final startup assignments, matched by PCI bus ID or UUID |

Memory attribution prefers NVML per-process VRAM when available, then logged
buffer allocations, then an estimate from free-VRAM changes since the logged
baseline. That estimate can be skewed by other processes. Windows WDDM commonly
lacks NVML per-process VRAM; Docker deliberately avoids matching container PIDs
to host NVML PIDs. Logged weights/context/work buffers describe allocations,
including mapped or pinned CPU buffers, rather than current physical residency.
System RAM/RSS and total GPU usage are separate live readings. Layer assignments
come from complete final startup records, not memory sizes or requested offload
flags; provisional memory-fit trials and draft/projector layers are excluded.

## Polling: log-driven when idle (no console spam)

Hitting llama-server's HTTP endpoints wakes its request loop, which at idle logs
`update_slots: all slots are idle` — once per poll. To avoid that, when a
`--llama-log` is configured the dashboard **watches the log file instead of
polling** while idle:

- **Idle** → only the log file (+ NVML for GPUs) is read; **llama-server is not
  contacted for routine stats after the initial bootstrap scrape**, so its loop
  is not repeatedly woken. The browser refreshes backend stats about every 3 s;
  explicit readiness/smoke checks still contact the managed server.
- A request appears in the log (`launch_slot … processing task`) → switches to
  **active**: polls `/slots` every 1 s for live KV fill and decode tok/s, until
  `/slots` reports idle again.
- On completion the log's `print_timing` and `draft acceptance` lines give the
  **exact** prefill/decode tok/s and speculative-decode acceptance (more precise
  than the `/metrics` gauges).

One background reader ingests complete log lines about once per second. The
dashboard uses its activity signal, memory startup data, and saved generations;
opening or closing a browser tab does not affect collection. The `/metrics`
throughput values are scrape-window gauges, while the **Last request** timings
come from the newest complete observed generation. The dashboard also shows
the current deferred-request count from `/metrics`.

The log parsing is intentionally tolerant (matches short, stable substrings and
treats any unrecognised growth as activity). If no `--llama-log` is set, or the
file is unavailable, it **falls back** to HTTP adaptive polling (about 1 s active /
3 s idle, with `/slots` and cached metrics at idle; model properties can also
refresh). These intervals are delays after a response, not fixed deadlines. The header shows
`idle (log)` or `idle (http)` so you can see which mode is active.

## Activity history

Open **History** in the navigation to filter by model, state, or date and
sort by time, total duration, prompt tokens, or generated tokens. Each record
shows its run, slot, and task when known. Missing timing lines remain marked
**incomplete**; timing lines without a slot are **ambiguous** and are never
combined with another slot's timings. Log unavailability or rotation can cause
a visible coverage gap. External logs start recording when first attached,
from the current end of the file.

For managed launches, enable **Save prompts in Activity history** in the
configuration, click **Save** to keep the option for future launches, then
relaunch the server. The option is saved per configuration in
`~/.llama-monitor/state.json`; toggling it alone only edits the current form.
Reloading while a managed server is running restores the configuration it was
actually launched with, including any edits that have not been saved to a named
configuration. When its binary supports
`--log-prompts-dir`, llama-monitor reads the prompt files and saves their text
in SQLite. Select **View prompt** in a history row to read it. The native files
contain the server's rendered prompt, which may include private text; they are
not copies of the full HTTP request. They do not contain responses or task IDs.
If simultaneous requests cannot be linked to timing rows with confidence,
their prompts appear as separate **Prompt only** entries. External servers can
use `--llama-prompts-dir` to point llama-monitor at an existing prompt directory.

History is stored at `~/.llama-monitor/history.sqlite` using SQLite WAL mode.
There is no automatic expiry or row limit; **Clear history…** deletes saved
generation and prompt records after confirmation, removes managed prompt files,
and keeps the current log cursor so old lines do not reappear. External prompt
files and the raw server log retain their existing lifecycles. JSONL currently
wraps ordinary server messages, so history does not claim to include every HTTP
request, status code, or response body.

See the [history API](docs/api.md#history-and-prompts) and
[CLI history filters](docs/cli.md#history-filters) for querying records, pagination,
prompt retrieval, and clearing stored history.

## Inference proxy and request history

Enable **Manage → Inference proxy → Enable inference proxy**, then change your
client base URL from llama-server (for example `http://localhost:8001/v1`) to
`http://localhost:8501/v1`. Native completion clients can use `/completion` on
that same proxy port. The proxy follows the current managed model or external
`--llama-url` target. Direct llama-server connections and existing monitoring
continue to work; direct traffic cannot have responses captured.

The inference listener is separate from the dashboard and exposes no monitor
management routes. It forwards HTTP methods, paths, query strings, authentication,
bodies, upstream status codes, and CORS headers. Streaming output arrives as it
is generated; disconnecting closes the upstream connection. Requests are never
automatically retried or moved to a different model midstream. Stopped/switching
targets return an unavailable response. Listener failures leave the dashboard usable.

**Save request prompts** and **Save model responses** start enabled. These
independent Manage toggles persist and apply to new requests without relaunching
the model. They do not change the separate native prompt-file setting. Capture
includes streaming and non-streaming `/v1/chat/completions`, `/v1/completions`,
and `/completion`, including reasoning and tool calls. Other HTTP endpoints pass
through without readable content capture; no additional upstream routing is done.

Open **Requests** for paired prompts/responses, model/endpoint/outcome/date filters,
HTTP status, duration, first observed generated content, usage when supplied,
and request IDs. First-token timing for non-streaming JSON is observed when the
complete JSON response is received, not a measurement of upstream token generation.
Activity **History** remains the separate source for server-log timings; requests
are never matched to generations by timestamp. There is no automatic refresh of
the Requests list: use **Refresh** to inspect recently completed requests.

Proxy records share `history.sqlite` but have their own **Clear requests** action.
They remain until cleared. Authentication headers and query strings are not saved.
Image/binary inputs are replaced with omission markers. Each saved payload is
limited to 1 MiB; truncation affects capture only. Incomplete, cancelled, or
interrupted records can contain partial output. If capture fails, inference
continues and Manage/CLI report a warning. This is best-effort local observability,
not a guaranteed audit log. The dashboard controls access to saved content under
its existing trusted-network deployment model.

```sh
python cli.py proxy status --json
python cli.py proxy settings --enabled on --json
python cli.py proxy smoke --stream --timeout 60 --json
python cli.py requests list --limit 10 --json
python cli.py requests show REQUEST_ID --json
python cli.py proxy settings --log-responses off --json
python cli.py requests clear --yes --json
```

In Docker, prefix these commands with `docker compose exec -T monitor`.
Compose publishes `PROXY_PORT=8501` on `PROXY_BIND_ADDRESS=127.0.0.1` by default;
use `0.0.0.0` for LAN clients and recreate the service after changing publication.
Native `--proxy-host` defaults to the dashboard bind address and `--proxy-port`
defaults to 8501 (`MONITOR_PROXY_HOST` / `MONITOR_PROXY_PORT` environment overrides).
Choose different dashboard, inference, and proxy ports. The proxy forwards client
credentials to the upstream; it adds no independent API-key or TLS layer.
`proxy smoke --api-key-env NAME` reads an upstream key from the CLI environment.
When the externally published proxy address differs, set `LLAMA_MONITOR_PROXY_URL`
to its root URL, such as `http://server:9001` (without `/v1`).

## Automatic Windows startup (native installation)

Open **Manage → Windows startup** and click **Set up Windows startup**. The app
creates and verifies the scheduled task for you; no manual Task Scheduler editing
is needed. Approve the Windows administrator prompt. **When Windows boots** also
asks for your Windows account password in a native Windows dialog (not your PIN).
The password is never sent to the dashboard, written into configuration/task XML,
or passed on a command line. Windows stores the task's credential. **When I sign
in** uses your existing interactive session and requires no stored password.

By default, setup remembers the running managed model and the registered embedding
server (or discovers the configured executable on port 8081). Start the models
you want before setup; their exact current arguments and working directories are
saved, including unsaved launch settings. Clear the model checkbox for a
controller-only task. Update setup to capture a different boot configuration.

The task starts `pythonw.exe` hidden under your own account, 30 seconds after boot
or 10 seconds after sign-in. It has no runtime limit, ignores duplicate starts,
and retries backend failures every minute, up to 999 times. Background output
goes to `~/.llama-monitor/backend.log` with bounded rotation. No browser window
opens. Installation applies on the next boot or sign-in and does not restart
your currently running backend or AI servers.

Model autostart runs once per Windows boot. Existing running servers are retained;
restarting the backend does not undo an explicit Stop in the same boot. Moonlight
connections take priority over startup loading. When gaming integration is
enabled, unknown Apollo status holds model loading. Persisted gaming recovery
takes priority over the boot preset. Loading uses the existing readiness checks,
five-minute timeout, and one automatic retry after 60 seconds.

**Remove Windows startup** removes only the task installed for this Windows account
and disables boot model loading. It does not stop current servers or remove Apollo
hooks. Setup refuses to replace an unrelated task. Reapprove boot setup if your
Windows password changes or the Python/repository paths move. Actual unattended
boot and GPU availability must be checked after installation and a reboot.

## Native Python installation (alternative)

Use this installation for Windows Apollo integration, Windows startup setup, or
direct access to host processes and paths. Docker is the primary installation.

**Prerequisites:** Git, Python (CI tests 3.11, 3.12, and 3.13), a separately installed
`llama-server` binary for your platform, and an existing `.gguf` model. Put the
binary on `PATH` or select it in **Manage**. Choose a llama.cpp build suitable for
your hardware; NVIDIA telemetry requires working NVIDIA drivers/NVML, while
CPU-only monitoring and inference do not require a GPU. No Node.js installation
is needed to use the committed UI. From PowerShell:

```powershell
git clone https://github.com/ridaken/llama-monitor.git
cd llama-monitor
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If you already cloned the repository, start with `cd` into that checkout. On
Linux/macOS use `python3 -m venv .venv` and `source .venv/bin/activate`, then the
same pip/backend commands. Apollo and automatic Windows startup remain Windows-only.

### Run

```powershell
python app.py
# open http://localhost:8500
```

Then use the **Monitor** view to start llama-server (see above). That's
the simplest path — the panel sets `--metrics` and `--log-file` for you.

The built React interface is included in `static/app`, so running the dashboard
requires only the Python dependencies. Node.js and pnpm are needed only for
[UI development](#development).

### Watching a server you started yourself (optional)

You can still launch llama-server manually and just point the dashboard at it.
For full telemetry and history, configure these on the **llama-server** side:

- `--metrics` — enables scrape-window throughput/request gauges. Slots can
  still supply live decode speed and estimated KV occupancy without it.
- `--log-file <path>` — supplies startup allocations, layer assignments, exact
  per-generation timings, history, and log-driven idle detection. For detailed
  layer placement, retain debug startup records (for example `--log-verbosity 5`
  if supported). Windows WDDM commonly lacks per-process VRAM telemetry, making
  the log especially useful for memory attribution.

Create the log directory before starting the external server. This example uses
Windows Command Prompt syntax for the server and PowerShell for the monitor:

```bat
llama-server.exe ^
  -m "...\Models\Qwen3.6-27B-UD-Q5_K_XL.gguf" ^
  -c 86000 --fit on -fa on --port 8001 ^
  --metrics ^
  --log-file "C:\llama-logs\llama.log"
```

```powershell
python app.py --llama-url http://localhost:8001 --llama-log "C:\llama-logs\llama.log"
```

### Options

| Flag | Default | Meaning |
|------|---------|---------|
| `--llama-url` | `http://localhost:8080` | Base URL of a server to watch. A non-default value is honored as an explicit "watch this" and takes precedence over re-adopting a panel-launched server; at the default, Launch from the panel retargets monitoring |
| `--llama-log` | _(none)_ | Path to that server's startup log (enables CPU split) |
| `--llama-prompts-dir` | _(none)_ | Directory written by an external server's `--log-prompts-dir` flag |
| `--port` | `8500` | Port for this dashboard |
| `--host` | `127.0.0.1` | Bind address |
| `--proxy-host` | Dashboard host | Proxy bind address; `MONITOR_PROXY_HOST` overrides the default |
| `--proxy-port` | `8501` | Proxy listener port; `MONITOR_PROXY_PORT` overrides the default |
| `--background` | Off | Use rotating `backend.log` in the data directory; Windows startup pairs this with `pythonw.exe` for a hidden process. It does not install a task or daemonize a foreground Python process by itself. |
| `--test-mode` | Off | Require a separate data directory, skip process adoption/integration automation, and stop test-owned models at shutdown; see [isolated tests](#isolated-development-tests). |

Env vars `LLAMA_URL`, `LLAMA_LOG`, `LLAMA_PROMPTS_DIR`, and `MONITOR_PORT` are also honored.
`LLAMA_MONITOR_DATA_DIR` overrides the native `~/.llama-monitor` storage directory.
The image sets it to `/data` and sets `LLAMA_MONITOR_CONTAINER=1` for container
behavior; native installations should leave container mode unset.

## Development

Use a separate Python environment and install the test dependencies from the
repository root:

```sh
python -m pip install -r requirements-dev.txt
```

CI exercises Python 3.11, 3.12, and 3.13; its frontend checks use Node.js 24 and
pnpm 11.25.0 (also pinned by `frontend/package.json`). To develop or rebuild the UI:

```sh
cd frontend
pnpm install --frozen-lockfile
pnpm dev          # UI at http://127.0.0.1:5173; run the backend separately
pnpm format:check
pnpm build        # typechecks and writes committed assets in ../static/app
pnpm exec playwright install chromium
pnpm test:e2e     # Chromium tests against an isolated FastAPI instance
```

The dev proxy targets a backend on port 8500. Use separate test data when running
that backend; never reuse normal native data for test mode. Commit rebuilt
`static/app` assets with UI changes. Browser tests cover UI behavior; CLI checks
cover backend controls and verification. The following commands run from the
repository root, after returning from `frontend`.

### Isolated development tests

Run the existing automated tests, then the Compose smoke harness to rebuild and
exercise the bundled CLI against a fake inference server. The harness uses a
unique project, temporary model directory, dedicated data volume, and free host
ports; it cleans up only its own resources and needs no real GGUF or GPU:

```sh
python -m pytest -q tests
python tests/container_smoke.py
python tests/native_proxy_smoke.py
# Optional NVIDIA image/integration check:
python tests/container_smoke.py --gpu
```

For native testing, use a separate data directory and backend port. PowerShell:

```powershell
$env:LLAMA_MONITOR_DATA_DIR = "$PWD/test-results/cli-native"
python app.py --test-mode --port 8765
# In another terminal:
python cli.py --url http://127.0.0.1:8765 wait --backend --json
python cli.py --url http://127.0.0.1:8765 status --json
```

Test mode rejects the default native data directory, skips persisted process
adoption, gaming recovery, and Windows startup actions, disables integration
changes, and stops test-owned models on graceful backend shutdown. Inherited
`LLAMA_URL`, `LLAMA_LOG`, and `LLAMA_PROMPTS_DIR` are ignored; explicit watch
arguments still apply. Choose an unused model port in test configurations. For
real-model verification, use an existing local model only when that test is in
scope; it can compete with other processes for GPU memory. Tests with the fake
server do not establish real GPU or model compatibility.

## Notes

- Scrape-window throughput gauges need llama-server's `--metrics` flag; live
  decode speed comes from slots and last-request timings from logs.
- GPU telemetry supports NVIDIA through NVML. AMD/ROCm or Metal telemetry would
  need another collector; CPU-only use is supported. The supplied Docker images
  offer CPU/CUDA inference, not ROCm or Metal.
- The launcher manages a single llama-server instance at a time.
- Settings and saved launch configs live in `~/.llama-monitor/state.json`; the
  managed log for launched servers is `~/.llama-monitor/llama-server.log`.
