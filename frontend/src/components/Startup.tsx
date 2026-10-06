import { useEffect, useState } from 'react'
import { api, post } from '@/api'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import type { StartupState } from '@/types'

export function Startup() {
  const [state, setState] = useState<StartupState | null>(null)
  const [mode, setMode] = useState<'boot' | 'logon'>('boot')
  const [models, setModels] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    let live = true
    let fetching = false
    let initialized = false
    const poll = async () => {
      if (fetching) return
      fetching = true
      try {
        const next = await api<StartupState>('/api/startup/state')
        if (live) {
          setState(next)
          if (!initialized) {
            setMode(next.mode || 'boot')
            if (next.installed) setModels(next.autostart_models)
            initialized = true
          }
        }
      } catch {
        if (live) setError('Could not read Windows startup settings.')
      } finally {
        fetching = false
      }
    }
    poll()
    const timer = setInterval(poll, 2000)
    return () => {
      live = false
      clearInterval(timer)
    }
  }, [])
  const setup = async (remove = false) => {
    setBusy(true)
    setError('')
    try {
      setState(
        await post<StartupState>(
          remove ? '/api/startup/remove' : '/api/startup/install',
          { mode, autostart_models: models },
        ),
      )
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Windows setup failed.')
    } finally {
      setBusy(false)
    }
  }
  const disabled = !state?.supported || !!state?.pending || busy
  return (
    <Card className="surface rounded-2xl" id="startup-panel">
      <CardHeader>
        <CardTitle>Windows startup</CardTitle>
        <CardDescription>
          Keep llama-monitor running in the background. Setup creates the
          Windows task, enables crash recovery, and keeps the dashboard
          optional.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <p id="startup-status" role="status" className="text-sm">
          {state?.pending
            ? 'Waiting for Windows setup to finish…'
            : state?.installed
              ? `Installed · ${state.mode === 'boot' ? 'starts before sign-in' : 'starts at sign-in'}`
              : 'Not installed'}
        </p>
        {state && !state.supported && (
          <p className="text-sm text-muted-foreground">
            Automatic task installation is available on Windows.
          </p>
        )}
        <div className="max-w-sm space-y-2">
          <Label htmlFor="startup-mode">Start llama-monitor</Label>
          <Select
            value={mode}
            onValueChange={(value) => setMode(value as 'boot' | 'logon')}
            disabled={disabled}
          >
            <SelectTrigger id="startup-mode">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="boot">
                When Windows boots (before sign-in)
              </SelectItem>
              <SelectItem value="logon">When I sign in</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="flex items-start gap-2">
          <Checkbox
            id="startup-models"
            checked={models}
            onCheckedChange={(value) => setModels(value === true)}
            disabled={disabled}
          />
          <Label htmlFor="startup-models" className="leading-5">
            Also start the currently running AI model and embedding server at
            boot
          </Label>
        </div>
        <p className="text-xs text-muted-foreground">
          Start the models you want first, then install or update startup to
          remember their exact settings. Existing running servers are kept.
          Moonlight takes priority, and backend restarts do not undo an explicit
          Stop.
        </p>
        <div className="flex flex-wrap gap-2">
          <Button
            id="startup-install"
            disabled={disabled}
            onClick={() => setup()}
          >
            {state?.installed
              ? 'Update Windows startup'
              : 'Set up Windows startup'}
          </Button>
          <Button
            id="startup-remove"
            variant="outline"
            disabled={disabled || !state?.installed}
            onClick={() => setup(true)}
          >
            Remove Windows startup
          </Button>
        </div>
        <p className="text-xs text-muted-foreground">
          Windows asks for administrator approval once. Boot startup also asks
          for your Windows account password in a Windows dialog, not your PIN.
          Sign-in startup does not need a stored password. Your password is
          never sent through this dashboard.
        </p>
        {!!state?.models.length && (
          <p id="startup-saved-models" className="break-words text-sm">
            Remembered for boot:{' '}
            {state.models
              .map((model) => `${model.name} (${model.port})`)
              .join(' · ')}
          </p>
        )}
        {state?.last_result && (
          <p className="text-sm text-muted-foreground">{state.last_result}</p>
        )}
        {(error || state?.last_error) && (
          <p id="startup-error" role="alert" className="text-sm text-amber-300">
            {error || state?.last_error}
          </p>
        )}
        {state?.installed && (
          <p className="break-all text-xs text-muted-foreground">
            Task: {state.task_name} · Background log: {state.log_path}
          </p>
        )}
      </CardContent>
    </Card>
  )
}
