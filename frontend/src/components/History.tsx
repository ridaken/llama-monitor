import { useEffect, useMemo, useState } from 'react'
import {
  CalendarRange,
  ChevronLeft,
  ChevronRight,
  Database,
  Eye,
  Search,
  Trash2,
} from 'lucide-react'
import { api } from '@/api'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
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
import { fmt } from '@/lib/utils'
import type { Activity, HistoryPage } from '@/types'

type Filters = {
  model: string
  state: string
  from: string
  to: string
  sort: string
  order: string
}
const initialFilters: Filters = {
  model: '',
  state: '',
  from: '',
  to: '',
  sort: 'time',
  order: 'desc',
}
const stateLabels: Record<string, string> = {
  complete: 'Complete',
  incomplete: 'Incomplete',
  ambiguous: 'Ambiguous',
  prompt_only: 'Prompt only',
  running: 'Running',
  error: 'Error',
}

export function History({ active }: { active: boolean }) {
  const [filters, setFilters] = useState<Filters>(initialFilters)
  const [cursorStack, setCursorStack] = useState<Array<string | null>>([null])
  const [pageIndex, setPageIndex] = useState(0)
  const [result, setResult] = useState<HistoryPage | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [prompt, setPrompt] = useState<{
    item: Activity
    text: string
    truncated: boolean
  } | null>(null)
  const [clearOpen, setClearOpen] = useState(false)
  const [revision, setRevision] = useState(0)
  const params = useMemo(() => {
    const query = new URLSearchParams({
      sort: filters.sort,
      order: filters.order,
    })
    if (filters.model) query.set('model', filters.model)
    if (filters.state) query.set('state', filters.state)
    if (filters.from)
      query.set(
        'from_ts',
        String(new Date(`${filters.from}T00:00:00`).getTime() / 1000),
      )
    if (filters.to) {
      const end = new Date(`${filters.to}T00:00:00`)
      end.setDate(end.getDate() + 1)
      query.set('to_ts', String(end.getTime() / 1000))
    }
    if (cursorStack[pageIndex]) query.set('cursor', cursorStack[pageIndex]!)
    return query.toString()
  }, [filters, cursorStack, pageIndex])
  useEffect(() => {
    if (!active) return
    const controller = new AbortController()
    setLoading(true)
    api<HistoryPage>(`/api/history?${params}`, { signal: controller.signal })
      .then((data) => {
        setResult(data)
        setError('')
        setLoading(false)
      })
      .catch((err) => {
        if (err.name !== 'AbortError') {
          setError(`Could not load history: ${err.message}`)
          setLoading(false)
        }
      })
    return () => controller.abort()
  }, [active, params, revision])
  const change = (key: keyof Filters, value: string) => {
    setFilters((old) => ({ ...old, [key]: value }))
    setCursorStack([null])
    setPageIndex(0)
    setPrompt(null)
  }
  const viewPrompt = async (item: Activity) => {
    setPrompt({ item, text: 'Loading prompt…', truncated: false })
    try {
      const data = await api<{ prompt_text: string; truncated?: boolean }>(
        `/api/history/${encodeURIComponent(item.id)}/prompt`,
      )
      setPrompt({ item, text: data.prompt_text, truncated: !!data.truncated })
    } catch (err) {
      setPrompt({
        item,
        text: `Could not load prompt: ${(err as Error).message}`,
        truncated: false,
      })
    }
  }
  const clear = async () => {
    try {
      await api('/api/history', { method: 'DELETE' })
      setClearOpen(false)
      setPrompt(null)
      setCursorStack([null])
      setPageIndex(0)
      setRevision((n) => n + 1)
    } catch (err) {
      setError(`Could not clear history: ${(err as Error).message}`)
      setClearOpen(false)
    }
  }
  const coverage = result?.log_status
  const coverageText = coverage?.gap
    ? 'Coverage gap: a log segment was unavailable or rotated before it could be read.'
    : !coverage?.configured
      ? 'No timing log attached; prompt files can still be saved when configured.'
      : !coverage.available
        ? 'Log unavailable; live monitoring uses HTTP until it returns.'
        : coverage.prompts_configured
          ? 'Observed generations and native prompt files. Unmatched prompts appear as Prompt only.'
          : 'Observed generations. Enable prompt logging in Manage to save prompt text.'
  const models = result?.models || []
  const rows = result?.items || []
  return (
    <div className="space-y-5">
      <Card className="surface rounded-2xl">
        <CardHeader>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <CardTitle className="flex items-center gap-2 text-xl">
                <CalendarRange className="size-5 text-primary" />
                Activity history
              </CardTitle>
              <CardDescription className="mt-2">
                Observed generations and saved prompts. Response text is never
                saved.
              </CardDescription>
            </div>
            <Button
              id="history-clear"
              variant="outline"
              onClick={() => setClearOpen(true)}
            >
              <Trash2 />
              Clear history
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          <p
            id="history-status"
            role="status"
            className={`rounded-lg border px-4 py-3 text-sm ${error || coverage?.gap ? 'border-amber-700/60 bg-amber-950/30 text-amber-200' : 'border-border bg-background/40 text-muted-foreground'}`}
          >
            {error || coverageText}
          </p>
        </CardContent>
      </Card>
      <Card className="surface rounded-2xl">
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Search className="size-4 text-primary" />
            Explore records
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="mb-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
            <div className="space-y-2">
              <Label htmlFor="history-model">Model</Label>
              <Select
                value={filters.model || 'all'}
                onValueChange={(value) =>
                  change('model', value === 'all' ? '' : value)
                }
              >
                <SelectTrigger id="history-model">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All models</SelectItem>
                  {models.map((model) => (
                    <SelectItem key={model} value={model}>
                      {model}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="history-state">State</Label>
              <Select
                value={filters.state || 'all'}
                onValueChange={(value) =>
                  change('state', value === 'all' ? '' : value)
                }
              >
                <SelectTrigger id="history-state">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All states</SelectItem>
                  {Object.entries(stateLabels).map(([value, label]) => (
                    <SelectItem key={value} value={value}>
                      {label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="history-from">From</Label>
              <Input
                id="history-from"
                type="date"
                value={filters.from}
                onChange={(e) => change('from', e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="history-to">To</Label>
              <Input
                id="history-to"
                type="date"
                value={filters.to}
                onChange={(e) => change('to', e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="history-sort">Sort by</Label>
              <Select
                value={filters.sort}
                onValueChange={(value) => change('sort', value)}
              >
                <SelectTrigger id="history-sort">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="time">Time</SelectItem>
                  <SelectItem value="duration">Total duration</SelectItem>
                  <SelectItem value="prompt_tokens">Prompt tokens</SelectItem>
                  <SelectItem value="generated_tokens">
                    Generated tokens
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="history-order">Order</Label>
              <Select
                value={filters.order}
                onValueChange={(value) => change('order', value)}
              >
                <SelectTrigger id="history-order">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="desc">Descending</SelectItem>
                  <SelectItem value="asc">Ascending</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          {loading && !result ? (
            <p className="p-8 text-center text-sm text-muted-foreground">
              Loading activity…
            </p>
          ) : (
            <>
              <div id="history-mobile" className="space-y-3 sm:hidden">
                {rows.map((item) => (
                  <article
                    key={item.id}
                    className="rounded-xl border border-border bg-background/30 p-4"
                  >
                    <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
                      <div>
                        <p className="text-xs text-muted-foreground">
                          {new Date(item.observed_at * 1000).toLocaleString()}
                        </p>
                        <h3 className="mt-1 break-all font-semibold">
                          {item.model || 'Unknown model'}
                        </h3>
                      </div>
                      <Badge
                        variant={
                          item.state === 'complete' ? 'default' : 'secondary'
                        }
                      >
                        {stateLabels[item.state] || item.state}
                      </Badge>
                    </div>
                    <dl className="grid grid-cols-2 gap-x-3 gap-y-2 text-xs">
                      <div>
                        <dt className="text-muted-foreground">
                          Run / slot / task
                        </dt>
                        <dd className="mono mt-1">
                          {item.run_id.slice(0, 8)} / {item.slot_id ?? '?'} /{' '}
                          {item.task_id ?? '?'}
                        </dd>
                      </div>
                      <div>
                        <dt className="text-muted-foreground">Prompt tokens</dt>
                        <dd className="mt-1">{fmt(item.prompt_tokens)}</dd>
                      </div>
                      <div>
                        <dt className="text-muted-foreground">Generated</dt>
                        <dd className="mt-1">{fmt(item.generated_tokens)}</dd>
                      </div>
                      <div>
                        <dt className="text-muted-foreground">Prefill</dt>
                        <dd className="mt-1">
                          {item.prompt_seconds == null
                            ? '—'
                            : `${fmt(item.prompt_seconds, 2)}s`}
                        </dd>
                      </div>
                      <div>
                        <dt className="text-muted-foreground">Decode</dt>
                        <dd className="mt-1">
                          {item.decode_seconds == null
                            ? '—'
                            : `${fmt(item.decode_seconds, 2)}s`}
                        </dd>
                      </div>
                      <div>
                        <dt className="text-muted-foreground">Total</dt>
                        <dd className="mt-1">
                          {item.total_seconds == null
                            ? '—'
                            : `${fmt(item.total_seconds, 2)}s`}
                        </dd>
                      </div>
                      <div>
                        <dt className="text-muted-foreground">
                          Draft accepted / generated
                        </dt>
                        <dd className="mt-1">
                          {item.draft_generated == null
                            ? '—'
                            : `${item.draft_accepted}/${item.draft_generated}`}
                        </dd>
                      </div>
                    </dl>
                    {item.has_prompt && (
                      <Button
                        variant="outline"
                        size="sm"
                        className="mt-4"
                        onClick={() => viewPrompt(item)}
                      >
                        <Eye />
                        View prompt
                      </Button>
                    )}
                  </article>
                ))}
                {!rows.length && (
                  <p className="rounded-xl border border-border p-8 text-center text-sm text-muted-foreground">
                    No observed generations match these filters.
                  </p>
                )}
              </div>
              <div className="hidden overflow-x-auto rounded-xl border border-border sm:block">
                <Table className="min-w-[1100px]">
                  <TableHeader>
                    <TableRow>
                      <TableHead>Observed</TableHead>
                      <TableHead>Model</TableHead>
                      <TableHead>State</TableHead>
                      <TableHead>Run / slot / task</TableHead>
                      <TableHead>Prompt</TableHead>
                      <TableHead>Prompt tokens</TableHead>
                      <TableHead>Generated</TableHead>
                      <TableHead>Prefill</TableHead>
                      <TableHead>Decode</TableHead>
                      <TableHead>Total</TableHead>
                      <TableHead>Draft</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody id="history-body">
                    {rows.map((item) => (
                      <TableRow key={item.id}>
                        <TableCell className="whitespace-nowrap">
                          {new Date(item.observed_at * 1000).toLocaleString()}
                        </TableCell>
                        <TableCell
                          className="max-w-44 truncate"
                          title={item.model || ''}
                        >
                          {item.model || '—'}
                        </TableCell>
                        <TableCell>
                          <Badge
                            variant={
                              item.state === 'complete'
                                ? 'default'
                                : 'secondary'
                            }
                          >
                            {stateLabels[item.state] || item.state}
                          </Badge>
                        </TableCell>
                        <TableCell className="mono text-xs">
                          {item.run_id.slice(0, 8)} / {item.slot_id ?? '?'} /{' '}
                          {item.task_id ?? '?'}
                        </TableCell>
                        <TableCell>
                          {item.has_prompt ? (
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={() => viewPrompt(item)}
                            >
                              <Eye />
                              View prompt
                            </Button>
                          ) : (
                            '—'
                          )}
                        </TableCell>
                        <TableCell>{fmt(item.prompt_tokens)}</TableCell>
                        <TableCell>{fmt(item.generated_tokens)}</TableCell>
                        <TableCell>
                          {item.prompt_seconds == null
                            ? '—'
                            : `${fmt(item.prompt_seconds, 2)}s`}
                        </TableCell>
                        <TableCell>
                          {item.decode_seconds == null
                            ? '—'
                            : `${fmt(item.decode_seconds, 2)}s`}
                        </TableCell>
                        <TableCell>
                          {item.total_seconds == null
                            ? '—'
                            : `${fmt(item.total_seconds, 2)}s`}
                        </TableCell>
                        <TableCell>
                          {item.draft_generated == null
                            ? '—'
                            : `${item.draft_accepted}/${item.draft_generated}`}
                        </TableCell>
                      </TableRow>
                    ))}
                    {!rows.length && (
                      <TableRow>
                        <TableCell
                          colSpan={11}
                          className="py-12 text-center text-muted-foreground"
                        >
                          No observed generations match these filters.
                        </TableCell>
                      </TableRow>
                    )}
                  </TableBody>
                </Table>
              </div>
              <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
                <span
                  id="history-path"
                  className="mono flex items-center gap-2 break-all text-xs text-muted-foreground"
                >
                  <Database className="size-4 shrink-0" />
                  {result?.database_path || '—'}
                </span>
                <div className="flex items-center gap-2">
                  <Button
                    id="history-prev"
                    variant="outline"
                    size="sm"
                    disabled={pageIndex === 0}
                    onClick={() => setPageIndex((n) => n - 1)}
                  >
                    <ChevronLeft />
                    Previous
                  </Button>
                  <span className="text-xs text-muted-foreground">
                    Page {pageIndex + 1}
                  </span>
                  <Button
                    id="history-next"
                    variant="outline"
                    size="sm"
                    disabled={!result?.next_cursor}
                    onClick={() => {
                      if (result?.next_cursor) {
                        setCursorStack((old) => {
                          const next = [...old]
                          next[pageIndex + 1] = result.next_cursor
                          return next
                        })
                        setPageIndex((n) => n + 1)
                      }
                    }}
                  >
                    Next
                    <ChevronRight />
                  </Button>
                </div>
              </div>
            </>
          )}
        </CardContent>
      </Card>
      <Dialog open={!!prompt} onOpenChange={(open) => !open && setPrompt(null)}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle>Saved prompt</DialogTitle>
            <DialogDescription>
              {prompt?.item.state === 'prompt_only'
                ? 'No reliable timing match for this prompt. '
                : ''}
              {prompt?.truncated ? 'First 1 MiB saved; prompt was longer.' : ''}
            </DialogDescription>
          </DialogHeader>
          <pre
            id="history-prompt-text"
            className="mono max-h-[60vh] overflow-auto rounded-lg border border-border bg-background p-4 text-xs whitespace-pre-wrap break-words"
          >
            {prompt?.text}
          </pre>
        </DialogContent>
      </Dialog>
      <Dialog open={clearOpen} onOpenChange={setClearOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Clear activity history?</DialogTitle>
            <DialogDescription>
              Delete saved generations and prompt text? This cannot be undone.
              Managed prompt files will also be removed; external server files
              remain.
            </DialogDescription>
          </DialogHeader>
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => setClearOpen(false)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={clear}>
              Clear history
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  )
}
