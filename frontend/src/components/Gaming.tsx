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
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { gamingLabel } from '@/lib/gaming'
import type { GamingState } from '@/types'

export function Gaming() {
  const [state, setState] = useState<GamingState | null>(null)
  const [url, setUrl] = useState('https://localhost:47990')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [port, setPort] = useState('8081')
  const [message, setMessage] = useState('')
  const [pending, setPending] = useState(false)
  useEffect(() => {
    let live = true
    let fetching = false
    let initialized = false
    const poll = async () => {
      if (fetching) return
      fetching = true
      try {
        const next = await api<GamingState>('/api/gaming/state')
        if (live) {
          setState(next)
          if (!initialized) {
            setUrl(next.apollo_url || 'https://localhost:47990')
            initialized = true
          }
        }
      } catch {
        if (live) setMessage('Could not reach Moonlight integration settings.')
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
  const action = async (path: string, body: unknown = {}) => {
    setPending(true)
    setMessage('')
    try {
      const next = await post<GamingState>(path, body)
      setState((previous) => ({ ...previous, ...next }))
      if (path.endsWith('/settings')) setPassword('')
      setMessage(
        path.endsWith('/test')
          ? 'Apollo connection verified. Its local certificate is now trusted.'
          : 'Integration settings updated.',
      )
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Operation failed.')
    } finally {
      setPending(false)
    }
  }
  const blocked = !!state?.blocked || pending
  return (
    <Card className="surface rounded-2xl" id="gaming-panel">
      <CardHeader>
        <CardTitle>Moonlight gaming integration</CardTitle>
        <CardDescription>
          Release AI memory on connection. Restore the previous models after 60
          seconds; retry failed loads once after another 60 seconds.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-muted-foreground">
          Requires the{' '}
          <a
            href="https://github.com/ridaken/Apollo"
            target="_blank"
            rel="noreferrer"
            className="underline"
          >
            Apollo build with independent authentication sessions
          </a>
          . Your Apollo browser login stays separate from background monitoring.
          Incompatible builds are detected before any login is attempted.
        </p>
        <p id="gaming-status" role="status" className="text-sm">
          {gamingLabel(state || undefined) ||
            (state?.enabled ? 'Enabled · AI available' : 'Disabled')}
          {state?.enabled && state.connected_clients != null
            ? ` · ${state.connected_clients} connected client(s)`
            : ''}
        </p>
        {state?.integration_error && (
          <p role="alert" className="text-sm text-amber-300">
            {state.integration_error}
          </p>
        )}
        <div className="grid gap-3 sm:grid-cols-3">
          <div>
            <Label htmlFor="gaming-url">Apollo local URL</Label>
            <Input
              id="gaming-url"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              disabled={blocked}
            />
          </div>
          <div>
            <Label htmlFor="gaming-user">Apollo username</Label>
            <Input
              id="gaming-user"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              disabled={blocked}
            />
          </div>
          <div>
            <Label htmlFor="gaming-password">Apollo password</Label>
            <Input
              id="gaming-password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              disabled={blocked}
              placeholder={
                state?.credentials_saved
                  ? 'Saved securely · leave blank to keep'
                  : 'Required for connection tracking'
              }
            />
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            id="gaming-save"
            variant="secondary"
            disabled={blocked}
            onClick={() =>
              action('/api/gaming/settings', {
                apollo_url: url,
                username,
                password,
              })
            }
          >
            Save connection
          </Button>
          <Button
            id="gaming-test"
            variant="outline"
            disabled={blocked || !state?.credentials_saved}
            onClick={() => action('/api/gaming/test')}
          >
            Test and trust local Apollo
          </Button>
          <Button
            id="gaming-enable"
            disabled={blocked || !state?.certificate_sha256}
            onClick={() =>
              action('/api/gaming/settings', { enabled: !state?.enabled })
            }
          >
            {state?.enabled
              ? 'Disable integration'
              : 'Enable for connection testing'}
          </Button>
        </div>
        {state?.certificate_sha256 && (
          <p className="break-all text-xs text-muted-foreground">
            Trusted certificate SHA-256: {state.certificate_sha256}
          </p>
        )}
        <div className="flex flex-wrap items-end gap-2">
          <div>
            <Label htmlFor="gaming-embedding-port">Embedding server port</Label>
            <Input
              className="w-32"
              id="gaming-embedding-port"
              type="number"
              value={port}
              onChange={(e) => setPort(e.target.value)}
              disabled={blocked}
            />
          </div>
          <Button
            id="gaming-register"
            variant="outline"
            disabled={blocked}
            onClick={() =>
              action('/api/gaming/auxiliary', { port: Number(port) })
            }
          >
            Register running embedding server
          </Button>
          {state?.auxiliary && (
            <p className="text-sm text-muted-foreground">
              Registered on port {state.auxiliary.port}
            </p>
          )}
        </div>
        {!!state?.servers?.length && (
          <div className="space-y-2" id="gaming-servers">
            {state.servers.map((server) => (
              <div
                key={server.id}
                className="flex flex-wrap items-center gap-2 text-sm"
              >
                <span>
                  {server.name} · {server.status} · attempt {server.attempts}/2
                </span>
                {server.error && (
                  <span className="text-amber-300">{server.error}</span>
                )}
                <Button
                  size="sm"
                  variant="outline"
                  disabled={pending || server.status === 'cancelled'}
                  onClick={() =>
                    action(`/api/gaming/servers/${server.id}/stop`)
                  }
                >
                  Stop / cancel restore
                </Button>
              </div>
            ))}
          </div>
        )}
        {state?.phase === 'failed' && (
          <div className="flex gap-2">
            <Button
              id="gaming-retry"
              disabled={pending || state.connected_clients !== 0}
              onClick={() => action('/api/gaming/retry')}
            >
              Retry failed models
            </Button>
            <Button
              id="gaming-cancel"
              variant="outline"
              disabled={pending}
              onClick={() => action('/api/gaming/cancel')}
            >
              Cancel automatic restoration
            </Button>
          </div>
        )}
        {state?.commands && (
          <details className="text-sm">
            <summary className="cursor-pointer">
              Apollo installation commands
            </summary>
            <p className="my-2 text-muted-foreground">
              Save and test the connection, register embeddings, then run{' '}
              <code>python apollo_setup.py --install</code> from this repository
              with permission to write Apollo configuration. Restart Apollo. The
              installer backs up configuration and installs these global
              preparation/undo commands.
            </p>
            <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded-md bg-muted p-3 text-xs">{`Prepare:\n${state.commands.prepare}\n\nUndo:\n${state.commands['session-ended']}\n\nRollback:\npython apollo_setup.py --rollback`}</pre>
            <p className="mt-2 text-xs text-muted-foreground">
              Validate Desktop, Steam Big Picture, and reconnects before leaving
              the integration enabled. Games left open can prevent restoration.
              Active AI requests and KV caches do not survive switching.
            </p>
          </details>
        )}
        {message && (
          <p id="gaming-message" role="status" className="text-sm">
            {message}
          </p>
        )}
      </CardContent>
    </Card>
  )
}
