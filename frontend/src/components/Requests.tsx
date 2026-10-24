import { useEffect, useState } from 'react'
import { api } from '@/api'
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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'

type RequestRecord = {
  id: string
  observed_at: number
  endpoint: string
  target: string
  run_id?: string | null
  model?: string | null
  state: string
  status_code?: number | null
  duration_seconds?: number
  first_token_seconds?: number | null
  usage?: Record<string, unknown> | null
  prompt_logging: boolean
  response_logging: boolean
  request_truncated?: boolean
  response_truncated?: boolean
  capture_invalid?: boolean
  request_text?: string | null
  response_text?: string | null
}
type Page = {
  items: RequestRecord[]
  next_cursor: string | null
  capture_error?: string | null
}
const seconds = (value?: number | null) =>
  value == null ? '—' : `${value.toFixed(3)}s`

export function Requests({ active }: { active: boolean }) {
  const [filters, setFilters] = useState({
    model: '',
    endpoint: '',
    state: '',
    from: '',
    to: '',
  })
  const [cursors, setCursors] = useState<Array<string | null>>([null])
  const [pageIndex, setPageIndex] = useState(0)
  const [result, setResult] = useState<Page | null>(null)
  const [selected, setSelected] = useState<RequestRecord | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [clearOpen, setClearOpen] = useState(false)
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    if (!active) return
    const controller = new AbortController()
    const params = new URLSearchParams({ limit: '50' })
    for (const key of ['model', 'endpoint', 'state'] as const) {
      if (filters[key]) params.set(key, filters[key])
    }
    if (filters.from)
      params.set(
        'from_ts',
        String(new Date(`${filters.from}T00:00:00`).getTime() / 1000),
      )
    if (filters.to) {
      const end = new Date(`${filters.to}T00:00:00`)
      end.setDate(end.getDate() + 1)
      params.set('to_ts', String(end.getTime() / 1000))
    }
    if (cursors[pageIndex]) params.set('cursor', cursors[pageIndex]!)
    setLoading(true)
    api<Page>(`/api/requests?${params}`, { signal: controller.signal })
      .then((next) => {
        setResult(next)
        setError('')
      })
      .catch((err) => {
        if (err.name !== 'AbortError') setError(err.message)
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [active, filters, cursors, pageIndex, revision])
  const change = (key: keyof typeof filters, value: string) => {
    setFilters((old) => ({ ...old, [key]: value }))
    setCursors([null])
    setPageIndex(0)
    setResult(null)
  }
  const show = async (id: string) => {
    try {
      setSelected(
        await api<RequestRecord>(`/api/requests/${encodeURIComponent(id)}`),
      )
    } catch (err) {
      setError((err as Error).message)
    }
  }
  const clear = async () => {
    try {
      await api('/api/requests', { method: 'DELETE' })
      setSelected(null)
      setClearOpen(false)
      setCursors([null])
      setPageIndex(0)
      setRevision((value) => value + 1)
    } catch (err) {
      setError((err as Error).message)
    }
  }
  return (
    <div className="space-y-4">
      <Card className="surface rounded-2xl">
        <CardHeader>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <CardTitle>Proxy requests</CardTitle>
              <CardDescription className="mt-2">
                Paired prompts and responses observed through the inference
                proxy. Log-derived generation timings remain in History. Other
                HTTP endpoints pass through without content capture.
              </CardDescription>
            </div>
            <div className="flex gap-2">
              <Button
                variant="outline"
                disabled={loading}
                onClick={() => setRevision((value) => value + 1)}
              >
                Refresh
              </Button>
              <Button variant="outline" onClick={() => setClearOpen(true)}>
                Clear requests
              </Button>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          {(error || result?.capture_error) && (
            <p role="alert" className="mb-4 text-amber-300">
              {error || result?.capture_error}
            </p>
          )}
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
            {(['model', 'endpoint'] as const).map((key) => (
              <div key={key} className="space-y-2">
                <Label htmlFor={`requests-${key}`}>
                  {key === 'model' ? 'Model' : 'Endpoint'}
                </Label>
                <Input
                  id={`requests-${key}`}
                  placeholder={key === 'model' ? 'All models' : 'All endpoints'}
                  value={filters[key]}
                  onChange={(event) => change(key, event.target.value)}
                />
              </div>
            ))}
            <div className="space-y-2">
              <Label htmlFor="requests-state">Outcome</Label>
              <Select
                value={filters.state || 'all'}
                onValueChange={(value) =>
                  change('state', value === 'all' ? '' : value)
                }
              >
                <SelectTrigger id="requests-state">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All outcomes</SelectItem>
                  {[
                    'running',
                    'complete',
                    'cancelled',
                    'incomplete',
                    'interrupted',
                    'error',
                  ].map((value) => (
                    <SelectItem value={value} key={value}>
                      {value}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {(['from', 'to'] as const).map((key) => (
              <div key={key} className="space-y-2">
                <Label htmlFor={`requests-${key}`}>
                  {key === 'from' ? 'From date' : 'Through date'}
                </Label>
                <Input
                  id={`requests-${key}`}
                  type="date"
                  value={filters[key]}
                  onChange={(event) => change(key, event.target.value)}
                />
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
      <Card className="surface min-w-0 rounded-2xl">
        <CardContent className="pt-4">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Time</TableHead>
                <TableHead>Model / endpoint</TableHead>
                <TableHead>Outcome</TableHead>
                <TableHead>HTTP</TableHead>
                <TableHead>Duration</TableHead>
                <TableHead>First token</TableHead>
                <TableHead>Details</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {(result?.items || []).map((row) => (
                <TableRow key={row.id}>
                  <TableCell className="whitespace-nowrap">
                    {new Date(row.observed_at * 1000).toLocaleString()}
                  </TableCell>
                  <TableCell>
                    <div>{row.model || 'Unknown model'}</div>
                    <div className="text-xs text-muted-foreground">
                      {row.endpoint}
                    </div>
                  </TableCell>
                  <TableCell>{row.state}</TableCell>
                  <TableCell>{row.status_code ?? '—'}</TableCell>
                  <TableCell>{seconds(row.duration_seconds)}</TableCell>
                  <TableCell>{seconds(row.first_token_seconds)}</TableCell>
                  <TableCell>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => void show(row.id)}
                    >
                      View request
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
              {!result?.items.length && (
                <TableRow>
                  <TableCell
                    colSpan={7}
                    className="py-10 text-center text-muted-foreground"
                  >
                    {loading
                      ? 'Loading requests…'
                      : 'No proxy requests. Enable the proxy in Manage and point a client at its base URL.'}
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
          <div className="mt-4 flex items-center justify-end gap-3">
            <Button
              variant="outline"
              disabled={pageIndex === 0 || loading}
              onClick={() => setPageIndex((value) => value - 1)}
            >
              Previous
            </Button>
            <span className="text-sm">Page {pageIndex + 1}</span>
            <Button
              variant="outline"
              disabled={!result?.next_cursor || loading}
              onClick={() => {
                setCursors((old) => [
                  ...old.slice(0, pageIndex + 1),
                  result!.next_cursor,
                ])
                setPageIndex((value) => value + 1)
              }}
            >
              Next
            </Button>
          </div>
        </CardContent>
      </Card>
      <Dialog
        open={!!selected}
        onOpenChange={(open) => {
          if (!open) setSelected(null)
        }}
      >
        <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-4xl">
          <DialogHeader>
            <DialogTitle>Request details</DialogTitle>
            <DialogDescription className="break-all">
              {selected?.id}
            </DialogDescription>
          </DialogHeader>
          {selected && (
            <>
              <p className="break-all text-sm">
                {selected.state} · HTTP {selected.status_code ?? '—'} ·{' '}
                {selected.target}
              </p>
              {selected.state !== 'complete' && (
                <p className="text-sm text-amber-300">
                  This request did not complete successfully. Saved output may
                  be partial.
                </p>
              )}
              {selected.capture_invalid && (
                <p className="text-sm text-amber-300">
                  Some content could not be interpreted. The saved response may
                  be incomplete.
                </p>
              )}
              {(['request', 'response'] as const).map((side) => {
                const text = selected[`${side}_text`]
                return (
                  <section key={side} className="min-w-0 space-y-2">
                    <div className="flex items-center justify-between">
                      <h3 className="font-semibold">
                        {side === 'request'
                          ? 'Request prompt and settings'
                          : 'Model response'}
                      </h3>
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={!text}
                        onClick={async () => {
                          try {
                            await navigator.clipboard.writeText(text!)
                          } catch {
                            setError('Could not copy content.')
                          }
                        }}
                      >
                        Copy {side}
                      </Button>
                    </div>
                    {selected[`${side}_truncated`] && (
                      <p className="text-sm text-amber-300">
                        Truncated at the 1 MiB capture limit. Forwarded traffic
                        was unaffected.
                      </p>
                    )}
                    <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-background/70 p-3 text-xs">
                      {text ??
                        ((
                          side === 'request'
                            ? selected.prompt_logging
                            : selected.response_logging
                        )
                          ? 'Content unavailable or request still running.'
                          : 'Capture was disabled for this request.')}
                    </pre>
                  </section>
                )
              })}
              <h3 className="font-semibold">Usage</h3>
              <pre className="whitespace-pre-wrap text-xs">
                {selected.usage
                  ? JSON.stringify(selected.usage, null, 2)
                  : 'Not reported by upstream.'}
              </pre>
            </>
          )}
        </DialogContent>
      </Dialog>
      <Dialog open={clearOpen} onOpenChange={setClearOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Clear proxy request history?</DialogTitle>
            <DialogDescription>
              Delete all captured proxy requests, prompts and responses.
              Existing Activity history and server logs are kept.
            </DialogDescription>
          </DialogHeader>
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => setClearOpen(false)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={() => void clear()}>
              Delete all requests
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  )
}
