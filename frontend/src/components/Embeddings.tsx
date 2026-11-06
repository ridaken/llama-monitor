import { useState } from 'react'
import { useLauncher } from '@/hooks/useLauncher'
import {
  ConfigurationPanel,
  LauncherDialogs,
  LauncherFeedback,
} from '@/components/Launcher'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog'

const labels: Record<string, string> = {
  stopped: 'Stopped',
  waiting: 'Waiting for primary',
  loading: 'Loading',
  ready: 'Ready',
  cpu_retry: 'Retrying on CPU',
  failed: 'Failed',
  exited: 'Exited',
  running: 'Loading',
}

export function Embeddings() {
  const launcher = useLauncher('embeddings')
  const [configure, setConfigure] = useState(false)
  const [busy, setBusy] = useState(false)
  const { state, launchAction } = launcher
  const status = state?.status
  const active =
    status &&
    (!!status.pid ||
      ['waiting', 'loading', 'ready', 'cpu_retry', 'running'].includes(
        status.state,
      ))
  const action = async (name: 'launch' | 'stop' | 'restart') => {
    setBusy(true)
    try {
      await launchAction(name)
    } finally {
      setBusy(false)
    }
  }
  const port = status?.config?.port || launcher.draft.port || 8081
  const hostPort = state?.runtime?.embedding_published_port || port
  return (
    <section
      id="embedding-panel"
      aria-label="Embedding model"
      className="surface mb-2 flex flex-wrap items-center gap-x-4 gap-y-2 rounded-xl px-3 py-2"
    >
      <div className="min-w-0 flex-1 basis-64 space-y-1">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <h2 className="text-sm font-semibold">Embedding model</h2>
          <span
            id="embedding-status"
            role="status"
            className="text-xs text-muted-foreground"
          >
            {status?.state === 'ready' && status.mode === 'cpu'
              ? 'Running on CPU'
              : labels[status?.state || 'stopped']}
          </span>
          <span
            className="truncate text-xs"
            title={status?.config?.name || state?.settings.default_config || ''}
          >
            {status?.config?.name ||
              state?.settings.default_config ||
              'No configuration selected'}
          </span>
        </div>
        <p className="break-all text-xs text-muted-foreground">
          Endpoint: http://{location.hostname}:{hostPort}/v1/embeddings
        </p>
        {status &&
          ['waiting', 'failed', 'cpu_retry'].includes(status.state) && (
            <p className="break-words text-xs text-amber-300">
              {status.reason}
            </p>
          )}
      </div>
      <div className="flex flex-wrap gap-2 [&>button]:h-7 [&>button]:px-2 [&>button]:text-xs">
        <Button
          id="embedding-start"
          size="sm"
          disabled={busy || !state?.binary_valid || launcher.gamingBlocked}
          onClick={() => action('launch')}
        >
          Start
        </Button>
        <Button
          id="embedding-stop"
          size="sm"
          variant="secondary"
          disabled={busy || !active}
          onClick={() => action('stop')}
        >
          Stop
        </Button>
        <Button
          id="embedding-restart"
          size="sm"
          variant="secondary"
          disabled={busy || !status?.config || launcher.gamingBlocked}
          onClick={() => action('restart')}
        >
          Restart
        </Button>
        <Button
          id="embedding-configure"
          size="sm"
          variant="outline"
          onClick={() => setConfigure(true)}
        >
          Configure
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={() => launcher.setModal('console')}
        >
          Logs
        </Button>
      </div>
      <LauncherFeedback launcher={launcher} target="launch" />
      <Dialog open={configure} onOpenChange={setConfigure}>
        <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-3xl">
          <DialogHeader>
            <DialogTitle>Configure embedding model</DialogTitle>
            <DialogDescription>
              Use the shared /models mount in Docker. Native installations use
              paths accessible to the backend.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <ConfigurationPanel launcher={launcher} />
            <div className="flex items-center gap-3">
              <Checkbox
                id="embedding-autostart"
                disabled={!state || launcher.savingEmbeddingSettings}
                checked={state?.settings.autostart_after_primary === true}
                onCheckedChange={(value) =>
                  launcher.saveEmbeddingSettings({
                    autostart_after_primary: value === true,
                  })
                }
              />
              <Label htmlFor="embedding-autostart">
                Start the default embedding configuration after primary is ready
              </Label>
            </div>
            <div className="flex items-center gap-3">
              <Checkbox
                id="embedding-cpu-fallback"
                disabled={!state || launcher.savingEmbeddingSettings}
                checked={state?.settings.cpu_fallback === true}
                onCheckedChange={(value) =>
                  launcher.saveEmbeddingSettings({
                    cpu_fallback: value === true,
                  })
                }
              />
              <Label htmlFor="embedding-cpu-fallback">
                Retry failed embedding loads on CPU
              </Label>
            </div>
            <p className="text-xs text-muted-foreground">
              Set a saved embedding configuration as default for automatic
              startup. CPU fallback makes one attempt and keeps your saved GPU
              settings.
            </p>
          </div>
        </DialogContent>
      </Dialog>
      <LauncherDialogs launcher={launcher} />
    </section>
  )
}
