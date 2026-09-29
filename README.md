# llama-monitor

A local web dashboard for a `llama.cpp` / `llama-server` session, with a React and
shadcn/ui interface. The **Monitor**, **Manage**, and **History** views keep live
status visible while you move between them.
It can **launch and manage** llama-server for you (browse to a `.gguf`, set
flags, Launch / Stop / Restart, and save/load named configurations), and shows,
live (1s refresh):

- **Model loaded** — name, context size, slot usage, KV-cache usage as
  `tokens / n_ctx (percent)`
- **Throughput** — prompt (pp) and decode tokens/sec, with a 60s smoothed decode
  sparkline (with Y-axis), speculative-decode efficiency, and last-prefill timing
- **Memory split** — how the model is divided across GPUs
  (e.g. `CUDA0: 10.0 GB · CUDA1: 12.0 GB`)
- **Slots** — per-slot state (idle / prefill / generating), context fill, prompt
  length and tokens generated
- **GPU hardware** — per-GPU temp/util/power sparklines, VRAM, and total draw
- **System memory** — system RAM used/total with a sparkline, plus the
  llama-server process's resident set size (where a model spills over when it
  doesn't fit in VRAM)
- **Activity history** — sortable, filterable generations observed in the log,
  retained in local SQLite until you clear them

### Speculative decoding (MTP / draft)

If the server runs with speculative decoding (e.g. `--spec-type draft-mtp`), the
Throughput panel shows **accepted tokens per step, per sequence** (≈1 means
speculation isn't helping; up to `spec_draft_n_max + 1` is ideal). If speculation
isn't in use, the section shows a "disabled — MTP not in use" notice instead.

Designed to run on the **same machine** as llama-server and the GPUs.

## Launch & manage llama-server

The **Manage** view runs llama-server for
you, so you don't need a separate launch script:

- **Pick the binary** — the path to `llama-server` is auto-detected from your
  `PATH` on first run and shown in the panel. If it can't be found you get a
  warning and a Browse button to point at the executable. Your choice is
  persisted across sessions.
- **Pick a model** — paste a path to a `.gguf` or **Browse** the filesystem.
- **Set flags** — add flags from the dropdown, which lists **every flag your
  installed llama-server supports** (parsed live from `llama-server --help`,
  alphabetised), or type any flag/value by hand. Each known flag shows a
  description next to it — whether picked from the dropdown or typed as a custom
  flag — and its value hint becomes the input placeholder. The Flags section
  **collapses** (click the Flags label) to free up dashboard space; collapsed
  it shows the currently-set flags as a read-only list — expand it to edit. The
  collapsed/expanded choice is remembered across sessions.
- **Console** — the **Console** button opens a live, auto-scrolling view of
  llama-server's console output (it tails the server's log file).
- **Port** — defaults to `8001`; change it if you like, but it can't be removed
  (the dashboard needs it to know where to monitor).
- **Launch / Stop / Restart** — llama-monitor starts the server and immediately
  **retargets its own monitoring** at it. A status pill shows
  `running` / `stopped` / `exited (code N)`.

llama-monitor **injects three flags** at known values so monitoring works:
`--port <your port>`, `--metrics`, and `--log-file` (pointed at
`~/.llama-monitor/llama-server.log`). When the installed binary advertises
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

> A launched server is left running when you close the dashboard — it's spawned
> detached, so killing/Ctrl+C-ing the dashboard (or closing its console) does
> **not** take the server down. Stop it explicitly from Manage. Closing or
> reloading the tab prompts only when configuration edits are unsaved; the server
> keeps running either way.
> If you restart the dashboard while a launched server is still running, it
> **re-adopts** that server automatically (status, monitoring, and Stop/Restart
> all reconnect) — unless you pass an explicit `--llama-url` (see below), which
> means "watch exactly this" and takes precedence over re-adoption. Single
> instance: launching replaces any server the panel previously started.

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
| GPU portion of the split | NVML per-process VRAM, matched to the llama-server PID |
| CPU portion of the split | parsed from llama-server's startup log (optional) |

llama.cpp has no runtime "GB per device" API, so the GPU split is read live from
NVML per-process memory, and the CPU/system-RAM portion is read from the startup
log if you pass `--llama-log`.

## Polling: log-driven when idle (no console spam)

Hitting llama-server's HTTP endpoints wakes its request loop, which at idle logs
`update_slots: all slots are idle` — once per poll. To avoid that, when a
`--llama-log` is configured the dashboard **watches the log file instead of
polling** while idle:

- **Idle** → only the log file (+ NVML for GPUs) is read; **llama-server is not
  contacted at all**, so its loop is never woken (no idle log spam). Polled every
  3 s.
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
file is unavailable, it **falls back** to HTTP adaptive polling (1 s active /
3 s idle, with a lightweight `/slots`-only idle poll). The header shows
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
configuration, then relaunch the server. When its binary supports
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

For local integrations, `GET /api/history` accepts `model`, `state`, `from_ts`,
`to_ts`, `sort` (`time`, `duration`, `prompt_tokens`, `generated_tokens`),
`order` (`asc` or `desc`), `limit`, and an opaque `cursor` returned as
`next_cursor`. `DELETE /api/history` clears saved generations while retaining
the log cursor. `GET /api/history/{id}/prompt` returns saved text when available.

## Setup

```powershell
cd C:\Users\Tom\Documents\Repos\llama-monitor
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Run

```powershell
python app.py
# open http://localhost:8500
```

Then use the **Manage** view to start llama-server (see above). That's
the simplest path — the panel sets `--metrics` and `--log-file` for you.

The built React interface is included in `static/app`, so running the dashboard
requires only the Python dependencies above. To develop or rebuild the UI, use
Node.js and pnpm:

```powershell
cd frontend
pnpm install --frozen-lockfile
pnpm dev        # UI at http://127.0.0.1:5173; run python app.py separately for /api
pnpm build      # writes the committed assets in ../static/app
pnpm test:e2e   # Chromium browser tests against an isolated FastAPI instance
```

### Watching a server you started yourself (optional)

You can still launch llama-server manually and just point the dashboard at it.
Two things must then be present on the **llama-server** side:

- `--metrics` — enables the `/metrics` endpoint (throughput, KV usage). Without
  it those panels stay blank (the dashboard tells you so).
- `--log-file <path>` — writes the startup log, which is the **only** source for
  the per-device memory split on Windows (NVML cannot report per-process VRAM
  under the WDDM driver model used by consumer GPUs).

```bat
llama-server.exe ^
  -m "...\Models\Qwen3.6-27B-UD-Q5_K_XL.gguf" ^
  -c 86000 --fit on -fa on --port 8001 ^
  --metrics ^
  --log-file "C:\Users\Tom\Desktop\llama-server-scripts\llama.log"
```

```powershell
python app.py --llama-url http://localhost:8001 --llama-log "C:\Users\Tom\Desktop\llama-server-scripts\llama.log"
```

### Options

| Flag | Default | Meaning |
|------|---------|---------|
| `--llama-url` | `http://localhost:8080` | Base URL of a server to watch. A non-default value is honored as an explicit "watch this" and takes precedence over re-adopting a panel-launched server; at the default, Launch from the panel retargets monitoring |
| `--llama-log` | _(none)_ | Path to that server's startup log (enables CPU split) |
| `--llama-prompts-dir` | _(none)_ | Directory written by an external server's `--log-prompts-dir` flag |
| `--port` | `8500` | Port for this dashboard |
| `--host` | `127.0.0.1` | Bind address |

Env vars `LLAMA_URL`, `LLAMA_LOG`, `LLAMA_PROMPTS_DIR`, and `MONITOR_PORT` are also honored.

## Notes

- Throughput needs llama-server's `--metrics` flag; without it the dashboard
  still shows the model and GPU stats.
- NVIDIA only (NVML). AMD/ROCm would need a different backend.
- The launcher manages a single llama-server instance at a time.
- Settings and saved launch configs live in `~/.llama-monitor/state.json`; the
  managed log for launched servers is `~/.llama-monitor/llama-server.log`.
