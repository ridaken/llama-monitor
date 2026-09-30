import { useEffect, useState } from 'react'
import {
  Activity,
  Clock3,
  History as HistoryIcon,
  Radio,
  Settings2,
} from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { History } from '@/components/History'
import { Manage } from '@/components/Manage'
import { Monitor } from '@/components/Monitor'
import { useMonitor } from '@/hooks/useMonitor'

type View = 'monitor' | 'manage' | 'history'
type MonitorLayout = 'fit' | 'comfortable'
const monitorLayoutKey = 'llama-monitor:monitor-layout'
const readMonitorLayout = (): MonitorLayout => {
  try {
    return localStorage.getItem(monitorLayoutKey) === 'comfortable'
      ? 'comfortable'
      : 'fit'
  } catch {
    return 'fit'
  }
}
const readView = (): View => {
  const hash = location.hash.slice(1)
  return hash === 'manage' || hash === 'history' ? hash : 'monitor'
}

export default function App() {
  const [view, setView] = useState<View>(readView)
  const [monitorLayout, setMonitorLayout] =
    useState<MonitorLayout>(readMonitorLayout)
  const monitor = useMonitor()
  useEffect(() => {
    try {
      localStorage.setItem(monitorLayoutKey, monitorLayout)
    } catch {
      // The layout still works when browser storage is unavailable.
    }
  }, [monitorLayout])
  useEffect(() => {
    const onHash = () => setView(readView())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])
  const data = monitor.data
  const online = !!data?.online && !monitor.failed
  const active = !!data?.active
  const connection = monitor.failed
    ? 'llama-server unreachable'
    : !monitor.updatedAt
      ? 'Connecting…'
      : online
        ? data?.metrics_enabled === false
          ? 'Online · enable --metrics for throughput'
          : 'llama-server online'
        : 'llama-server unreachable'
  const links: Array<{
    key: View
    label: string
    icon: typeof Activity
    description: string
  }> = [
    {
      key: 'monitor',
      label: 'Monitor',
      icon: Activity,
      description: 'Live telemetry',
    },
    {
      key: 'manage',
      label: 'Manage',
      icon: Settings2,
      description: 'Models and server',
    },
    {
      key: 'history',
      label: 'History',
      icon: HistoryIcon,
      description: 'Past activity',
    },
  ]
  return (
    <div className="min-h-screen bg-background">
      <header className="sticky top-0 z-40 border-b border-border/70 bg-background/95 backdrop-blur-xl">
        <div className="mx-auto flex max-w-[2400px] flex-wrap items-center gap-3 px-4 py-2 sm:px-6 lg:px-8">
          <a
            href="#monitor"
            className="focus-ring flex min-w-0 items-center gap-3 rounded-lg"
          >
            <span className="brand-mark flex size-9 shrink-0 items-center justify-center rounded-xl border border-primary/40 bg-primary/10 text-primary">
              <Radio className="size-5" />
            </span>
            <span className="min-w-0">
              <span className="block text-sm font-bold tracking-tight">
                llama-monitor
              </span>
              <span className="block text-[11px] text-muted-foreground">
                Local inference control
              </span>
            </span>
          </a>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <Badge
              id="status"
              variant="outline"
              className={`max-w-full gap-2 px-3 py-1.5 text-xs ${online ? 'border-teal-700/60 bg-teal-950/30 text-teal-300' : 'border-rose-700/60 bg-rose-950/30 text-rose-300'}`}
            >
              <span
                className={`size-2 shrink-0 rounded-full ${online ? 'bg-teal-400' : 'bg-rose-400'}`}
              />
              {connection}
            </Badge>
            <span
              id="model"
              className="hidden max-w-48 truncate text-xs text-muted-foreground lg:block"
              title={data?.model?.name || ''}
            >
              {data?.model?.name || ''}
            </span>
          </div>
        </div>
      </header>
      <div className="mx-auto max-w-[2400px] px-4 pb-10 sm:px-6 lg:px-8">
        <div className="flex flex-col gap-2 border-b border-border/60 py-3 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <div className="mb-1 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[.2em] text-primary">
              <span className="size-1.5 rounded-full bg-primary" />
              Workspace
            </div>
            <h1 className="text-xl font-semibold tracking-tight sm:text-2xl">
              {view === 'monitor'
                ? 'Live monitor'
                : view === 'manage'
                  ? 'Server management'
                  : 'Activity history'}
            </h1>
            <p className="mt-0.5 text-xs text-muted-foreground sm:text-sm">
              {view === 'monitor'
                ? 'A clear view of what your model and hardware are doing now.'
                : view === 'manage'
                  ? 'Launch, tune, and manage your llama-server session.'
                  : 'Find past generations and inspect saved prompts.'}
            </p>
          </div>
          <div
            id="updated"
            className="flex items-center gap-2 text-xs text-muted-foreground"
          >
            <Clock3 className="size-4" />
            {monitor.updatedAt
              ? `${monitor.updatedAt.toLocaleTimeString()} · ${active ? 'active' : `idle (${data?.log_mode ? 'log' : 'http'})`}`
              : 'Waiting for data'}
          </div>
        </div>
        <nav
          aria-label="Main navigation"
          className="mb-3 flex flex-wrap items-center gap-2 border-b border-border/60 py-2"
        >
          {links.map(({ key, label, icon: Icon, description }) => (
            <Button
              key={key}
              id={
                key === 'history'
                  ? 'history-open'
                  : key === 'manage'
                    ? 'manage-open'
                    : undefined
              }
              asChild
              variant={view === key ? 'secondary' : 'ghost'}
              className={
                view === key
                  ? 'accent-glow border border-primary/40 text-primary'
                  : 'text-muted-foreground'
              }
            >
              <a
                href={`#${key}`}
                aria-current={view === key ? 'page' : undefined}
                title={description}
              >
                <Icon />
                {label}
              </a>
            </Button>
          ))}
          {view === 'monitor' && (
            <div
              role="group"
              aria-label="Monitor layout"
              className="ml-auto flex items-center gap-1 rounded-lg border border-border bg-muted/40 p-1"
            >
              {(['fit', 'comfortable'] as const).map((layout) => (
                <Button
                  key={layout}
                  type="button"
                  size="sm"
                  variant={monitorLayout === layout ? 'secondary' : 'ghost'}
                  aria-pressed={monitorLayout === layout}
                  onClick={() => setMonitorLayout(layout)}
                  className={
                    monitorLayout === layout
                      ? 'accent-glow border border-primary/40 text-primary'
                      : 'text-muted-foreground'
                  }
                >
                  {layout === 'fit' ? 'Fit' : 'Comfortable'}
                </Button>
              ))}
            </div>
          )}
        </nav>
        <main>
          <div hidden={view !== 'monitor'}>
            <Monitor monitor={monitor} layout={monitorLayout} />
          </div>
          <div hidden={view !== 'manage'}>
            <Manage />
          </div>
          <div hidden={view !== 'history'}>
            <History active={view === 'history'} />
          </div>
        </main>
        <footer className="mt-12 flex flex-wrap items-center justify-between gap-3 border-t border-border/50 pt-6 text-xs text-muted-foreground">
          <span>Local llama-server monitoring</span>
          <span>Data stays on this machine</span>
        </footer>
      </div>
    </div>
  )
}
