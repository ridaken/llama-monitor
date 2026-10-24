import { Activity, CircuitBoard, Cpu, Gauge, Zap } from 'lucide-react'
import {
  ConfigurationPanel,
  LaunchControls,
  LaunchStatus,
} from '@/components/Launcher'
import type { LauncherController } from '@/hooks/useLauncher'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Progress } from '@/components/ui/progress'
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip'
import { clamp, fmt, fmtGB } from '@/lib/utils'
import type { Stats, Timing } from '@/types'
import type { useMonitor } from '@/hooks/useMonitor'
import { useMonitorFit } from '@/hooks/useMonitorFit'

type MonitorData = ReturnType<typeof useMonitor>
type MemoryEntry = NonNullable<Stats['split']>[number]
const memoryNotes: Record<string, string> = {
  nvml: 'Live llama-server VRAM measured by NVML. Teal is llama-server; orange is the rest of current device usage.',
  log: 'Buffer sizes from the load log: weights, KV cache and work buffers. CPU includes mapped files and pinned RAM. These sizes are not live physical usage.',
  'nvml-delta':
    'Estimated change in VRAM since model load. Other apps can affect this value; it is not a measured process allocation.',
}

function Info({ children, label }: { children: string; label: string }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          aria-label={`More about ${label}`}
          className="ml-1 inline-flex size-4 items-center justify-center rounded-full border border-muted-foreground/60 text-[10px] text-muted-foreground focus-ring"
        >
          i
        </button>
      </TooltipTrigger>
      <TooltipContent className="max-w-64 leading-relaxed">
        {children}
      </TooltipContent>
    </Tooltip>
  )
}

function Panel({
  title,
  icon: Icon,
  info,
  children,
  className = '',
}: {
  title: string
  icon: typeof Activity
  info: string
  children: React.ReactNode
  className?: string
}) {
  return (
    <Card className={`surface monitor-panel min-w-0 rounded-2xl ${className}`}>
      <CardHeader className="monitor-panel-header pb-3">
        <CardTitle className="flex items-center gap-2 text-sm font-semibold tracking-wide text-slate-200">
          <Icon className="size-4 text-primary" />
          {title}
          <Info label={title}>{info}</Info>
        </CardTitle>
      </CardHeader>
      <CardContent className="monitor-panel-content">{children}</CardContent>
    </Card>
  )
}

function Row({
  label,
  value,
  id,
  info,
}: {
  label: string
  value: React.ReactNode
  id?: string
  info?: string
}) {
  return (
    <div className="monitor-row flex items-start justify-between gap-4 border-b border-border/45 py-2 last:border-0">
      <span className="text-sm text-muted-foreground">
        {label}
        {info && <Info label={label}>{info}</Info>}
      </span>
      <span id={id} className="text-right text-sm font-medium tabular-nums">
        {value}
      </span>
    </div>
  )
}

function ModelMemory({
  entry,
  source,
  capacity,
}: {
  entry?: MemoryEntry
  source?: string | null
  capacity?: number | null
}) {
  if (!entry) return null
  const label =
    source === 'nvml'
      ? 'llama-server VRAM'
      : source === 'log'
        ? 'llama.cpp buffers · logged'
        : 'llama-server · estimate'
  return (
    <div className="model-memory">
      <Row
        label={label}
        value={fmtGB(entry.bytes)}
        info={memoryNotes[source || '']}
      />
      {capacity != null && entry.bytes > capacity && (
        <p className="text-xs text-amber-400" role="status">
          Exceeds device capacity; not resident VRAM.
        </p>
      )}
    </div>
  )
}

function VramBar({
  used,
  total,
  memory,
  source,
}: {
  used?: number | null
  total?: number | null
  memory?: MemoryEntry
  source?: string | null
}) {
  const measured =
    source === 'nvml' &&
    memory &&
    used != null &&
    total != null &&
    total > 0 &&
    memory.bytes >= 0 &&
    memory.bytes <= used &&
    used <= total
  const pct = total && used != null ? clamp((used / total) * 100) : 0
  if (!measured) return <Progress value={pct} className="mt-2 h-1.5" />
  return (
    <>
      <div
        className="vram-bar mt-2 flex h-1.5 overflow-hidden rounded-full bg-primary/20"
        role="img"
        aria-label={`VRAM: llama-server ${fmtGB(memory.bytes)}, other usage ${fmtGB(used - memory.bytes)}, capacity ${fmtGB(total)}`}
      >
        <span
          className="vram-model"
          style={{ width: `${(memory.bytes / total) * 100}%` }}
        />
        <span
          className="bg-primary"
          style={{ width: `${((used - memory.bytes) / total) * 100}%` }}
        />
      </div>
      <div className="vram-legend mt-1 flex flex-wrap gap-x-3 text-xs text-muted-foreground">
        <span>
          <i className="vram-model inline-block size-2 rounded-sm" />{' '}
          llama-server
        </span>
        <span>
          <i className="inline-block size-2 rounded-sm bg-primary" /> Other
          usage
        </span>
      </div>
    </>
  )
}

function Spark({
  values,
  color = '#ff6a00',
  max,
  axis = false,
  label,
}: {
  values: number[]
  color?: string
  max?: number
  axis?: boolean
  label: string
}) {
  const width = 320,
    height = axis ? 90 : 42,
    left = axis ? 30 : 2,
    right = width - 3,
    top = 5,
    bottom = height - (axis ? 20 : 4)
  const ceiling = Math.max(1, max || 0, ...values)
  const points = values
    .map(
      (v, i) =>
        `${right - ((values.length - 1 - i) / 59) * (right - left)},${bottom - (Math.min(v, ceiling) / ceiling) * (bottom - top)}`,
    )
    .join(' ')
  return (
    <svg
      role="img"
      aria-label={label}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      className={`monitor-spark w-full ${axis ? 'h-24' : 'h-11'}`}
    >
      {axis && (
        <>
          <line x1={left} y1={top} x2={right} y2={top} stroke="#304052" />
          <line
            x1={left}
            y1={(top + bottom) / 2}
            x2={right}
            y2={(top + bottom) / 2}
            stroke="#304052"
          />
          <line x1={left} y1={bottom} x2={right} y2={bottom} stroke="#304052" />
          <text x="2" y="11" fill="#9daec1" fontSize="9">
            {fmt(ceiling, ceiling < 10 ? 1 : 0)}
          </text>
          <text x="2" y={bottom + 3} fill="#9daec1" fontSize="9">
            0
          </text>
        </>
      )}
      {values.length > 1 && (
        <polyline
          points={points}
          fill="none"
          stroke={color}
          strokeWidth={axis ? 2.5 : 2}
          strokeLinecap="round"
          strokeLinejoin="round"
          vectorEffect="non-scaling-stroke"
        />
      )}
      {values.length === 1 && (
        <circle
          cx={right}
          cy={
            bottom - (Math.min(values[0], ceiling) / ceiling) * (bottom - top)
          }
          r="3"
          fill={color}
        />
      )}
    </svg>
  )
}

const timing = (value?: Timing, rate = true) =>
  value
    ? `${value.tokens.toLocaleString()} tok · ${value.secs.toFixed(2)}s${rate && value.tps ? ` (${value.tps.toFixed(0)} tok/s)` : ''}`
    : '—'

function LayerRows({
  placement,
  label,
}: {
  placement?: Stats['layer_placement']
  label?: string
}) {
  const entry = placement?.devices.find((device) => device.label === label)
  const value =
    placement?.status === 'no_model'
      ? 'No model loaded'
      : placement?.status === 'loading'
        ? 'Loading'
        : placement?.status === 'shared'
          ? 'Shared across devices'
          : placement?.status === 'available' && entry
            ? `${entry.layers} / ${placement.total_layers}`
            : 'Unavailable'
  return (
    <div className="layer-placement">
      <Row
        label="Assigned model layers"
        value={value}
        info="Primary device assignment for transformer layers, excluding the output layer. Expert or tensor overrides can place some weights on other devices; this is not a count of fully resident layers."
      />
      {placement?.status === 'available' && entry && (
        <Row
          label="Output layer"
          value={
            placement.output_device
              ? placement.output_device === label
                ? 'Assigned here'
                : 'Elsewhere'
              : 'Unavailable'
          }
        />
      )}
    </div>
  )
}

export function Monitor({
  monitor,
  launcher,
}: {
  monitor: MonitorData
  launcher: LauncherController
}) {
  const fitRef = useMonitorFit()
  const { data: d, samples } = monitor
  const model = d?.model || {}
  const slots = d?.slots
  const throughput = d?.throughput || {}
  const kv = d?.kv?.usage_ratio
  const kvTokens = d?.kv?.tokens
  const live = throughput.decode_tps_live
  const decode =
    live == null
      ? throughput.decode_tps_avg
      : samples.decode.length
        ? samples.decode.at(-1)
        : live
  const spec = d?.spec || {}
  const specText = !spec.enabled
    ? 'Disabled · MTP not in use'
    : spec.tokens_per_decode != null
      ? `${spec.tokens_per_decode.toFixed(2)} tok/step (live)`
      : spec.mean_len != null || spec.accept_rate != null
        ? `${spec.mean_len != null ? `${spec.mean_len.toFixed(2)} tok/step` : ''}${spec.accept_rate != null ? ` · ${(spec.accept_rate * 100).toFixed(0)}% accept` : ''} (last req)`
        : 'MTP on · idle'
  const split = d?.split || []
  const placement = d?.layer_placement
  const gpuIndices = new Set(d?.gpu?.devices?.map((device) => device.index))
  const unmatched = new Map(
    (placement?.devices || [])
      .filter((entry) => entry.label !== 'CPU' && entry.gpu_index == null)
      .map((entry) => [entry.label, entry]),
  )
  for (const entry of split) {
    if (entry.kind === 'gpu' && !gpuIndices.has(entry.gpu_index ?? -1)) {
      if (!unmatched.has(entry.label))
        unmatched.set(entry.label, { label: entry.label, layers: 0 })
    }
  }
  return (
    <TooltipProvider delayDuration={150}>
      <div ref={fitRef} className="monitor-layout">
        <div className="monitor-controls">
          <div className="surface subtle-grid monitor-feature monitor-hero relative overflow-hidden rounded-2xl p-6 sm:p-8">
            <div className="absolute -right-20 -top-28 size-64 rounded-full bg-primary/10 blur-3xl" />
            <div className="relative">
              <div className="monitor-session-heading flex flex-wrap items-center justify-between gap-2">
                <p className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[.2em] text-primary">
                  <Activity className="size-4" /> Live session
                </p>
                <LaunchStatus launcher={launcher} />
              </div>
              <h2
                id="s-model"
                className="break-all text-3xl font-semibold tracking-tight sm:text-4xl"
              >
                {model.name || 'Waiting for a model'}
              </h2>

              <div className="model-meta mt-4 flex flex-wrap gap-2">
                <Badge variant="secondary" className="px-3 py-1.5">
                  Context{' '}
                  <span id="s-ctx" className="ml-1 text-foreground">
                    {model.n_ctx?.toLocaleString() || '—'}
                  </span>
                </Badge>
                <Badge variant="secondary" className="px-3 py-1.5">
                  Busy slots{' '}
                  <span id="s-slots" className="ml-1 text-foreground">
                    {slots
                      ? `${slots.busy} / ${slots.total}`
                      : model.total_slots
                        ? `0 / ${model.total_slots}`
                        : '—'}
                  </span>
                </Badge>
              </div>
              <LaunchControls launcher={launcher} />
            </div>
          </div>
          <ConfigurationPanel launcher={launcher} />
        </div>
        <div className="monitor-telemetry">
          <div className="surface monitor-feature monitor-decode rounded-2xl p-6 sm:p-8">
            <p className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-[.2em] text-primary">
              <Zap className="size-4" /> Decode speed
            </p>
            <div className="metric text-5xl font-semibold">
              <span id="t-decode">{fmt(decode, 1)}</span>
              <span className="ml-2 text-base font-normal tracking-normal text-muted-foreground">
                tok/s
              </span>
            </div>
            <p className="mt-2 text-xs text-muted-foreground">
              Live · scrape window{' '}
              <span id="t-decode-avg">{fmt(throughput.decode_tps_avg, 1)}</span>
            </p>
            <Spark
              values={samples.decode}
              color="#ff6a00"
              label="Decode tokens per second over the last 60 samples"
            />
            <p className="text-xs text-muted-foreground">
              3-sample smoothed · last ~60s
            </p>
          </div>

          <Panel
            title="Session"
            icon={CircuitBoard}
            info="The loaded model, request lanes, and shared KV cache."
            className="monitor-session"
          >
            <Row
              label="Processing"
              id="s-req"
              value={fmt(d?.requests?.processing)}
              info="Requests generating now."
            />
            <Row
              label="Deferred"
              id="s-deferred"
              value={fmt(d?.requests?.deferred)}
              info="Requests waiting for a free slot."
            />
            <Row
              label="KV cache"
              id="s-kv-pct"
              value={
                kv != null
                  ? `${(kvTokens || 0).toLocaleString()}${model.n_ctx ? ` / ${model.n_ctx.toLocaleString()}` : ''} (${(kv * 100).toFixed(1)}%)`
                  : '—'
              }
              info="Tokens held across all slots, including retained prompts."
            />
            <Progress
              value={kv == null ? 0 : clamp(kv * 100)}
              className="mt-2 h-2"
            />
          </Panel>
          <Panel
            title="Throughput"
            icon={Gauge}
            info="Prompt processing and generation rates from llama-server metrics and live slots."
            className="monitor-throughput"
          >
            <Row
              label="Prompt processing"
              id="t-pp"
              value={`${fmt(throughput.pp_tps_avg, 1)} tok/s`}
            />
            <Row
              label="Speculative decode"
              id="t-spec"
              value={specText}
              info="Average accepted tokens per model step; exact last-request values come from the log."
            />
            <Row
              label="Last request"
              id="lr-total"
              value={timing(d?.last_request?.total, false)}
            />
            <Row
              label="Prompt"
              id="lr-pp"
              value={timing(d?.last_request?.pp)}
            />
            <Row
              label="Generation"
              id="lr-gen"
              value={timing(d?.last_request?.generation)}
            />
          </Panel>
          <Panel
            title="Slots"
            icon={Activity}
            info="Parallel inference lanes. Idle slots can retain a prompt cache."
            className="monitor-slots xl:col-span-2"
          >
            <div id="slots" className="grid gap-3 sm:grid-cols-2">
              {slots?.list?.length ? (
                slots.list.map((slot) => {
                  const pct =
                    slot.state === 'prefill'
                      ? (slot.prefill_ratio || 0) * 100
                      : (slot.ctx_ratio || 0) * 100
                  const meta =
                    slot.state === 'prefill'
                      ? `Prefilling ${(slot.prompt_processed || 0).toLocaleString()} / ${(slot.prompt_tokens || 0).toLocaleString()} prompt tokens`
                      : slot.state === 'generating'
                        ? `Prompt ${(slot.prompt_tokens || 0).toLocaleString()} · generated ${(slot.decoded || 0).toLocaleString()} · ctx ${pct.toFixed(1)}%`
                        : slot.ctx_used
                          ? `Idle · holds ${slot.ctx_used.toLocaleString()} tokens (${pct.toFixed(1)}% ctx)`
                          : 'Idle · empty'
                  return (
                    <div
                      key={slot.id}
                      className="monitor-slot rounded-xl border border-border/70 bg-background/30 p-4"
                    >
                      <div className="mb-3 flex items-center justify-between">
                        <strong>Slot {slot.id}</strong>
                        <Badge
                          variant={
                            slot.state === 'generating'
                              ? 'default'
                              : 'secondary'
                          }
                          className={
                            slot.state === 'prefill' ? 'text-amber-300' : ''
                          }
                        >
                          {slot.state}
                        </Badge>
                      </div>
                      <p className="mb-3 text-xs text-muted-foreground">
                        {meta}
                      </p>
                      <Progress value={clamp(pct)} className="h-1.5" />
                    </div>
                  )
                })
              ) : (
                <p className="text-sm text-muted-foreground">
                  No slot data yet.
                </p>
              )}
            </div>
          </Panel>
          <Panel
            title="Hardware"
            icon={Cpu}
            info={
              d?.runtime?.containerized
                ? 'Layer assignments, NVIDIA hardware, and RAM visible inside Docker (the Linux VM on Docker Desktop), not Windows host RAM. Trends show about 60 samples.'
                : 'Layer assignments, NVIDIA hardware, whole-machine RAM and llama-server resident memory. Trends show about 60 samples.'
            }
            className="monitor-hardware"
          >
            <div id="gpus" className="hardware-grid grid gap-4">
              <div className="monitor-cpu rounded-xl border border-border/70 bg-background/30 p-4">
                <strong className="mb-3 block">
                  {d?.runtime?.containerized ? 'CPU & Docker RAM' : 'CPU & RAM'}
                </strong>
                <LayerRows placement={placement} label="CPU" />
                <div id="sysmem">
                  {d?.sysmem?.ok ? (
                    <>
                      <div className="flex items-baseline justify-between">
                        <span className="text-sm text-muted-foreground">
                          RAM used
                        </span>
                        <strong className="metric text-2xl">
                          {fmt(
                            d.sysmem.percent ??
                              (d.sysmem.total
                                ? ((d.sysmem.used || 0) / d.sysmem.total) * 100
                                : 0),
                          )}
                          %
                        </strong>
                      </div>
                      <Spark
                        values={samples.sysmem}
                        max={100}
                        color="#9e86eb"
                        label="System RAM use over the last 60 samples"
                      />
                      <Row
                        label="RAM"
                        value={`${fmtGB(d.sysmem.used)} / ${fmtGB(d.sysmem.total)}`}
                      />
                      <div className="model-memory">
                        <Row
                          label="llama-server RSS"
                          value={fmtGB(d.sysmem.llama_rss)}
                          info="RSS (resident set size) is the system RAM currently resident for the detected llama-server processes, including resident model pages and runtime memory. It excludes GPU VRAM and can differ from buffer sizes recorded in the load log."
                        />
                      </div>
                    </>
                  ) : (
                    <p className="text-sm text-muted-foreground">
                      {d?.sysmem?.error || 'No system memory data.'}
                    </p>
                  )}
                </div>
                <ModelMemory
                  entry={split.find((entry) => entry.kind === 'cpu')}
                  source={d?.split_source}
                />
              </div>
              {(d?.gpu?.devices || []).map((device) => {
                const hist = samples.gpu[device.index]
                const memory = split.find(
                  (entry) => entry.gpu_index === device.index,
                )
                return (
                  <div
                    key={device.index}
                    className="monitor-gpu rounded-xl border border-border/70 bg-background/30 p-4"
                  >
                    <div className="mb-4 flex items-start justify-between gap-3">
                      <strong>
                        GPU {device.index} · {device.name || 'GPU'}
                      </strong>
                      <Badge
                        variant="secondary"
                        className="shrink-0 whitespace-nowrap"
                      >
                        {fmt(device.power, 0)} W
                      </Badge>
                    </div>
                    <LayerRows
                      placement={placement}
                      label={
                        placement?.devices.find(
                          (entry) => entry.gpu_index === device.index,
                        )?.label
                      }
                    />
                    <div className="space-y-3">
                      {(
                        [
                          ['Temp', 'temp', device.temp, 100, '#f17878', '°C'],
                          [
                            'Util',
                            'util',
                            device.util_gpu,
                            100,
                            '#ff6a00',
                            '%',
                          ],
                          [
                            'Power',
                            'power',
                            device.power,
                            device.power_limit || 1,
                            '#e5ad66',
                            'W',
                          ],
                        ] as const
                      ).map(([label, key, value, max, color, unit]) => (
                        <div
                          key={key}
                          className="monitor-gpu-trend grid items-center gap-2 text-xs"
                        >
                          <span className="text-muted-foreground">{label}</span>
                          <Spark
                            values={hist?.[key] || []}
                            max={max}
                            color={color}
                            label={`GPU ${device.index} ${label} trend`}
                          />
                          <span className="text-right mono">
                            {fmt(value, 0)} {unit}
                          </span>
                        </div>
                      ))}
                    </div>
                    <div className="mt-4 flex justify-between text-xs">
                      <span className="text-muted-foreground">VRAM</span>
                      <span className="mono">
                        {fmtGB(device.mem_used)} / {fmtGB(device.mem_total)}
                      </span>
                    </div>
                    <VramBar
                      used={device.mem_used}
                      total={device.mem_total}
                      memory={memory}
                      source={d?.split_source}
                    />
                    <ModelMemory
                      entry={memory}
                      source={d?.split_source}
                      capacity={device.mem_total}
                    />
                  </div>
                )
              })}
              {Array.from(unmatched.values()).map((entry) => (
                <div
                  key={entry.label}
                  className="monitor-placement rounded-xl border border-border/70 bg-background/30 p-4"
                >
                  <strong>
                    {entry.label}
                    {entry.name ? ` · ${entry.name}` : ''}
                  </strong>
                  <LayerRows placement={placement} label={entry.label} />
                  <ModelMemory
                    entry={split.find((memory) => memory.label === entry.label)}
                    source={d?.split_source}
                  />
                  <p className="mt-3 text-xs text-muted-foreground">
                    Hardware telemetry could not be matched to this device.
                  </p>
                </div>
              ))}
            </div>
            {(placement?.reason ||
              !d?.gpu?.ok ||
              !!d?.gpu?.devices?.length) && (
              <div className="monitor-hardware-footer mt-3 flex flex-wrap items-baseline gap-x-4 gap-y-1 text-sm text-muted-foreground">
                {(placement?.reason || !d?.gpu?.ok) && (
                  <div className="min-w-0 flex-1 basis-48 space-y-1 [overflow-wrap:anywhere]">
                    {placement?.reason && (
                      <p id="placement-note">{placement.reason}</p>
                    )}
                    {!d?.gpu?.ok && (
                      <p id="gpu-error">{d?.gpu?.error || 'No GPU data.'}</p>
                    )}
                  </div>
                )}
                {!!d?.gpu?.devices?.length && (
                  <p
                    id="gpu-total-w"
                    className="ml-auto shrink-0 whitespace-nowrap text-right"
                  >
                    Total draw{' '}
                    {fmt(
                      d.gpu.devices.reduce(
                        (sum, device) => sum + (device.power || 0),
                        0,
                      ),
                    )}{' '}
                    W
                  </p>
                )}
              </div>
            )}
          </Panel>
        </div>
      </div>
    </TooltipProvider>
  )
}
