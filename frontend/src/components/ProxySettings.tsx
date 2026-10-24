import { useEffect, useState } from 'react'
import { api, post } from '@/api'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'

type ProxyState = {
  enabled: boolean
  log_prompts: boolean
  log_responses: boolean
  host: string
  port: number
  target: string | null
  listening: boolean
  listener_error: string | null
  capture_error: string | null
}

export function ProxySettings() {
  const [state, setState] = useState<ProxyState | null>(null)
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  const [copied, setCopied] = useState(false)
  useEffect(() => {
    let live = true
    let fetching = false
    const poll = async () => {
      if (fetching) return
      fetching = true
      try {
        const next = await api<ProxyState>('/api/proxy/state')
        if (live) setState(next)
      } catch (err) {
        if (live) setError((err as Error).message)
      } finally {
        fetching = false
      }
    }
    void poll()
    const timer = setInterval(poll, 3000)
    return () => {
      live = false
      clearInterval(timer)
    }
  }, [])
  const change = async (
    key: 'enabled' | 'log_prompts' | 'log_responses',
    value: boolean,
  ) => {
    setPending(true)
    setError('')
    try {
      setState(await post<ProxyState>('/api/proxy/settings', { [key]: value }))
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setPending(false)
    }
  }
  const baseUrl = state ? `http://${location.hostname}:${state.port}/v1` : ''
  return (
    <Card className="surface rounded-2xl" id="proxy-panel">
      <CardHeader className="manage-panel-header">
        <CardTitle>Inference proxy</CardTitle>
      </CardHeader>
      <CardContent className="manage-panel-content space-y-4">
        {(
          [
            ['enabled', 'Enable inference proxy'],
            ['log_prompts', 'Save request prompts'],
            ['log_responses', 'Save model responses'],
          ] as const
        ).map(([key, label]) => (
          <div key={key} className="flex items-start gap-3">
            <Checkbox
              id={`proxy-${key}`}
              checked={state?.[key] === true}
              disabled={!state || pending}
              onCheckedChange={(value) => void change(key, value === true)}
            />
            <Label htmlFor={`proxy-${key}`} className="leading-5">
              {label}
            </Label>
          </div>
        ))}
        <p role="status" className="text-sm">
          {state?.listening
            ? 'Listening'
            : state?.enabled
              ? 'Not listening'
              : 'Disabled'}
          {state && ` · ${state.host}:${state.port}`}
        </p>
        <div className="space-y-2">
          <p className="text-sm text-muted-foreground">Client base URL</p>
          <code className="block break-all text-xs">
            {baseUrl || 'Loading…'}
          </code>
          <Button
            variant="outline"
            size="sm"
            disabled={!baseUrl}
            onClick={async () => {
              try {
                await navigator.clipboard.writeText(baseUrl)
                setCopied(true)
              } catch {
                setError('Could not copy the client URL.')
              }
            }}
          >
            {copied ? 'Copied' : 'Copy base URL'}
          </Button>
        </div>
        <p className="break-all text-sm">
          Upstream: {state?.target || 'Stopped or switching'}
        </p>
        <p className="text-sm text-muted-foreground">
          Point clients at this proxy to capture chat and completion requests in
          Requests. Direct llama-server connections continue to work. Capture
          changes apply to new requests. Records remain until cleared; each
          saved payload is limited to 1 MiB. Image and binary content is
          omitted.
        </p>
        <p className="text-xs text-muted-foreground">
          Docker: use the published PROXY_PORT if it differs from this address.
          Native: set the bind address and port at startup.
        </p>
        {(error || state?.listener_error || state?.capture_error) && (
          <p role="alert" className="text-sm text-amber-300">
            {error || state?.listener_error || state?.capture_error}
          </p>
        )}
      </CardContent>
    </Card>
  )
}
