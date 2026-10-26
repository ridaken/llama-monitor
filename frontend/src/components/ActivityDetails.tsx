import type { ActivityRecord, RequestRecord } from '@/types'
import { Button } from '@/components/ui/button'
import { fmt } from '@/lib/utils'

export function ProxyDetails({
  selected,
  setError,
}: {
  selected: RequestRecord
  setError: (message: string) => void
}) {
  return (
    <>
      {selected.state !== 'complete' && (
        <p className="text-sm text-amber-300">
          This request did not complete successfully. Saved output may be
          partial.
        </p>
      )}
      {selected.capture_invalid && (
        <p className="text-sm text-amber-300">
          Some content could not be interpreted. The saved response may be
          incomplete.
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
                Truncated at the 1 MiB capture limit. Forwarded traffic was
                unaffected.
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
  )
}

export function MonitoredDetails({ item }: { item: ActivityRecord }) {
  return (
    <>
      <p className="break-all text-sm">
        Run {item.run_id || 'Unknown'} · slot {item.slot_id ?? '—'} · task{' '}
        {item.task_id ?? '—'}
      </p>
      <dl className="grid grid-cols-2 gap-3 text-sm">
        {[
          ['Prompt tokens', fmt(item.prompt_tokens)],
          ['Generated tokens', fmt(item.generated_tokens)],
          [
            'Prefill',
            item.prompt_seconds == null
              ? '—'
              : `${fmt(item.prompt_seconds, 3)}s`,
          ],
          [
            'Decode',
            item.decode_seconds == null
              ? '—'
              : `${fmt(item.decode_seconds, 3)}s`,
          ],
          [
            'Generation duration',
            item.total_seconds == null ? '—' : `${fmt(item.total_seconds, 3)}s`,
          ],
          [
            'Draft accepted / generated',
            item.draft_generated == null
              ? '—'
              : `${item.draft_accepted ?? '—'}/${item.draft_generated}`,
          ],
          [
            'Draft acceptance rate',
            item.draft_accept_rate == null
              ? '—'
              : `${fmt(item.draft_accept_rate * 100, 1)}%`,
          ],
          ['Draft mean length', fmt(item.draft_mean_len, 2)],
        ].map(([label, value]) => (
          <div key={label}>
            <dt className="text-muted-foreground">{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
      {item.state === 'prompt_only' && (
        <p className="text-sm text-amber-300">
          No reliable timing match for this prompt.
        </p>
      )}
    </>
  )
}
