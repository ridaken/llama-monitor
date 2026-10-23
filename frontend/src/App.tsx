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
import { LauncherDialogs } from '@/components/Launcher'
import { useLauncher } from '@/hooks/useLauncher'
import { Manage } from '@/components/Manage'
import { Monitor } from '@/components/Monitor'
import { useMonitor } from '@/hooks/useMonitor'
import { gamingLabel } from '@/lib/gaming'

type View = 'monitor' | 'manage' | 'history'
type AppLayout = 'fit' | 'comfortable'
const appLayoutKey = 'llama-monitor:monitor-layout'
const readAppLayout = (): AppLayout => {
  try {
    return localStorage.getItem(appLayoutKey) === 'comfortable'
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
  const [appLayout, setAppLayout] = useState<AppLayout>(readAppLayout)
  const monitor = useMonitor()
  const launcher = useLauncher()
  useEffect(() => {
    try {
      localStorage.setItem(appLayoutKey, appLayout)
    } catch {
      // The layout still works when browser storage is unavailable.
    }
  }, [appLayout])
  useEffect(() => {
    const onHash = () => setView(readView())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])
  const data = monitor.data
  const online = !!data?.online && !monitor.failed
  const active = !!data?.active
  const connection =
    gamingLabel(data?.gaming) ||
    (monitor.failed
      ? 'llama-server unreachable'
      : !monitor.updatedAt
        ? 'Connecting…'
        : online
          ? data?.metrics_enabled === false
            ? 'Online · enable --metrics for throughput'
            : 'llama-server online'
          : 'llama-server unreachable')
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
    <div className={`app-shell app-${appLayout} min-h-dvh bg-background`}>
      <header className="sticky top-0 z-40 border-b border-border/70 bg-background/95 backdrop-blur-xl">
        <div className="flex w-full flex-wrap items-center gap-3 px-4 py-1.5 sm:px-6 lg:px-8">
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
          <div className="ml-auto flex min-w-0 max-w-full flex-wrap items-center gap-2">
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
            <div
              id="updated"
              className="flex min-w-0 max-w-full items-center gap-2 text-xs text-muted-foreground"
            >
              <Clock3 className="size-4 shrink-0" />
              <span>
                {monitor.updatedAt
                  ? `${monitor.updatedAt.toLocaleTimeString()} · ${active ? 'active' : `idle (${data?.log_mode ? 'log' : 'http'})`}`
                  : 'Waiting for data'}
              </span>
            </div>
          </div>
        </div>
      </header>
      <div className="app-body mx-auto w-full px-4 pb-4 sm:px-6 lg:px-8">
        <nav
          aria-label="Main navigation"
          className="mb-2 flex flex-wrap items-center gap-2 border-b border-border/60 py-1"
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
          <div
            role="group"
            aria-label="App layout"
            className="ml-auto flex items-center gap-1 rounded-lg border border-border bg-muted/40 p-1"
          >
            {(['fit', 'comfortable'] as const).map((layout) => (
              <Button
                key={layout}
                type="button"
                size="sm"
                variant={appLayout === layout ? 'secondary' : 'ghost'}
                aria-pressed={appLayout === layout}
                onClick={() => setAppLayout(layout)}
                className={
                  appLayout === layout
                    ? 'accent-glow border border-primary/40 text-primary'
                    : 'text-muted-foreground'
                }
              >
                {layout === 'fit' ? 'Fit' : 'Comfortable'}
              </Button>
            ))}
          </div>
        </nav>
        <main className="app-main">
          <h1 className="sr-only">
            {view === 'monitor'
              ? 'Live monitor'
              : view === 'manage'
                ? 'Server management'
                : 'Activity history'}
          </h1>
          <div className="monitor-view" hidden={view !== 'monitor'}>
            <Monitor monitor={monitor} launcher={launcher} />
          </div>
          <div hidden={view !== 'manage'}>
            <Manage launcher={launcher} />
          </div>
          <div hidden={view !== 'history'}>
            <History active={view === 'history'} />
          </div>
        </main>
        <LauncherDialogs launcher={launcher} />
        <footer className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-border/50 pt-3 text-xs text-muted-foreground">
          <span>Local llama-server monitoring</span>
          <span>Data stays on this machine</span>
        </footer>
      </div>
    </div>
  )
}
