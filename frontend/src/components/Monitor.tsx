import {
  Activity,
  Boxes,
  CircuitBoard,
  Cpu,
  Gauge,
  MemoryStick,
  Zap,
} from 'lucide-react'
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
import type { Timing } from '@/types'
import type { useMonitor } from '@/hooks/useMonitor'

type MonitorData = ReturnType<typeof useMonitor>
const splitColors = [
  '#ff6a00',
  '#9e86eb',
  '#56c5a8',
  '#e5ad66',
  '#de83aa',
  '#66c4d4',
]
const sourceNote: Record<string, string> = {
  nvml: 'Exact per-process VRAM from NVML',
  log: 'From llama-server load log',
  'nvml-delta':
    'Approximate VRAM change since model load; other apps can affect it',
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

export function Monitor({
  monitor,
  layout,
}: {
  monitor: MonitorData
  layout: 'fit' | 'comfortable'
}) {
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
  const splitTotal = split.reduce((sum, item) => sum + item.bytes, 0)
  return (
    <TooltipProvider delayDuration={150}>
      <div
        className={`monitor-layout monitor-${layout} ${layout === 'comfortable' ? 'space-y-6' : ''}`}
      >
        <div className="monitor-summary grid gap-4 lg:grid-cols-[1.35fr_.65fr]">
          <div className="surface subtle-grid monitor-feature monitor-hero relative overflow-hidden rounded-2xl p-6 sm:p-8">
            <div className="absolute -right-20 -top-28 size-64 rounded-full bg-primary/10 blur-3xl" />
            <div className="relative">
              <p className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-[.2em] text-primary">
                <Activity className="size-4" /> Live session
              </p>
              <h2
                id="s-model"
                className="break-all text-3xl font-semibold tracking-tight sm:text-4xl"
              >
                {model.name || 'Waiting for a model'}
              </h2>
              <p className="mt-3 text-sm text-muted-foreground">
                {d?.online
                  ? 'Current model and inference activity'
                  : 'Connect to llama-server to see live telemetry.'}
              </p>
              <div className="mt-7 flex flex-wrap gap-3">
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
            </div>
          </div>
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
        </div>
        <div className="monitor-panels grid gap-4 lg:grid-cols-2 xl:grid-cols-3">
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
            title="Memory split"
            icon={Boxes}
            info="Estimated model footprint by device; the source note explains accuracy."
            className="monitor-split"
          >
            <div
              id="split-bar"
              className="flex h-3 overflow-hidden rounded-full bg-muted"
            >
              {splitTotal > 0 &&
                split.map((item, i) => (
                  <span
                    key={`${item.label}-${i}`}
                    style={{
                      width: `${(item.bytes / splitTotal) * 100}%`,
                      background: splitColors[i % splitColors.length],
                    }}
                  />
                ))}
            </div>
            <div id="split-legend" className="mt-4 space-y-2">
              {splitTotal ? (
                split.map((item, i) => (
                  <div
                    key={`${item.label}-${i}`}
                    className="flex items-center justify-between text-sm"
                  >
                    <span className="flex items-center gap-2">
                      <span
                        className="size-2.5 rounded-sm"
                        style={{
                          background: splitColors[i % splitColors.length],
                        }}
                      />
                      {item.label}
                    </span>
                    <span className="mono">{fmtGB(item.bytes)}</span>
                  </div>
                ))
              ) : (
                <p className="text-sm text-muted-foreground">
                  {d?.split_log_configured
                    ? 'Waiting for model load information.'
                    : 'Attach a llama-server log to show the per-device split.'}
                </p>
              )}
            </div>
            <p id="split-note" className="mt-3 text-xs text-muted-foreground">
              {sourceNote[d?.split_source || ''] || ''}
            </p>
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
            title="System memory"
            icon={MemoryStick}
            info="Whole-machine RAM and the llama-server process resident memory."
            className="monitor-memory"
          >
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
                  <Row
                    label="llama-server RSS"
                    value={fmtGB(d.sysmem.llama_rss)}
                  />
                </>
              ) : (
                <p className="text-sm text-muted-foreground">
                  {d?.sysmem?.error || 'No system memory data.'}
                </p>
              )}
            </div>
          </Panel>
          <Panel
            title="GPUs"
            icon={Cpu}
            info="NVIDIA GPU temperature, utilization, power, and VRAM. Each trend shows about 60 samples."
            className="monitor-gpus xl:col-span-3"
          >
            <div id="gpus">
              {d?.gpu?.ok && d.gpu.devices?.length ? (
                <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                  {d.gpu.devices.map((device) => {
                    const hist = samples.gpu[device.index]
                    const memPct = device.mem_total
                      ? ((device.mem_used || 0) / device.mem_total) * 100
                      : 0
                    return (
                      <div
                        key={device.index}
                        className="monitor-gpu rounded-xl border border-border/70 bg-background/30 p-4"
                      >
                        <div className="mb-4 flex items-start justify-between gap-3">
                          <strong>
                            CUDA{device.index} · {device.name || 'GPU'}
                          </strong>
                          <Badge variant="secondary">
                            {fmt(device.power, 0)} W
                          </Badge>
                        </div>
                        <div className="space-y-3">
                          {(
                            [
                              [
                                'Temp',
                                'temp',
                                device.temp,
                                100,
                                '#f17878',
                                '°C',
                              ],
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
                              className="grid grid-cols-[48px_1fr_70px] items-center gap-2 text-xs"
                            >
                              <span className="text-muted-foreground">
                                {label}
                              </span>
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
                        <Progress
                          value={clamp(memPct)}
                          className="mt-2 h-1.5"
                        />
                      </div>
                    )
                  })}
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">
                  {d?.gpu?.error || 'No GPU data.'}
                </p>
              )}
            </div>
            <div
              id="gpu-total-w"
              className="mt-4 text-right text-sm text-muted-foreground"
            >
              {d?.gpu?.devices?.length
                ? `Total draw ${fmt(d.gpu.devices.reduce((sum, device) => sum + (device.power || 0), 0))} W`
                : ''}
            </div>
          </Panel>
        </div>
      </div>
    </TooltipProvider>
  )
}
