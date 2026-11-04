import type { ConsoleEntry } from '@/types'

export type ConsoleFilter = 'traces' | 'events' | 'errors'
type DisplayEntry = { text: string; category: ConsoleFilter }
export type ConsoleBuffer = {
  entries: DisplayEntry[]
  lastCategory: ConsoleFilter
  omitted: boolean
}
export const emptyConsoleBuffer = (): ConsoleBuffer => ({
  entries: [],
  lastCategory: 'traces',
  omitted: false,
})

const failure =
  /\b(?:error|fatal|critical|panic|warn(?:ing)?|failed|failure|exception|assert(?:ion)?|SIG(?:ABRT|BUS|FPE|ILL|INT|KILL|PIPE|QUIT|SEGV|TERM|TRAP)|segmentation fault|out of memory|traceback)\b/i
const activity =
  /processing task|stop processing|print_timing|(?:prompt )?eval time|total time\s*=|draft acceptance|mean acceptance|cancel(?:led|ing)? task|request.*(?:completed|finished|cancel(?:led|ing)?|started)|server (?:is listening|ready|launched|stopped|exited)|model (?:loaded|ready)|stop requested/i
const abnormalExit = /server exited:\s*code\s+(?!0\b)\S+/i
const limit = 512 * 1024

function category(entry: ConsoleEntry, previous: ConsoleFilter): ConsoleFilter {
  const level = entry.level?.toLowerCase()
  if (
    ['warn', 'warning', 'error', 'fatal', 'critical'].includes(level || '') ||
    /^(?:[\d:.+-]+\s+)?[EWF]\s/.test(entry.text) ||
    failure.test(entry.text) ||
    abnormalExit.test(entry.text)
  )
    return 'errors'
  if (entry.type === 'lifecycle' || activity.test(entry.text)) return 'events'
  if (
    !entry.text.trim() ||
    entry.continuation ||
    /^(?:\s|File |at )/.test(entry.text)
  )
    return previous
  return 'traces'
}

export function appendConsoleEntries(
  previous: ConsoleBuffer,
  incoming: ConsoleEntry[],
  replace: boolean,
): ConsoleBuffer {
  const base = replace ? emptyConsoleBuffer() : previous
  let lastCategory = base.lastCategory
  const added: DisplayEntry[] = []
  for (const entry of incoming) {
    const clean = entry.text.replace(/\x1b\[[0-9;]*m/g, '')
    const lines = clean.split('\n')
    // Each API entry is one record; an embedded trailing newline isn't an
    // additional blank record. Severity applies to every line of that record.
    if (lines.length > 1 && lines.at(-1) === '') lines.pop()
    for (const [index, text] of lines.entries()) {
      lastCategory = category(
        { ...entry, text, continuation: index > 0 || entry.continuation },
        lastCategory,
      )
      added.push({ text: text + '\n', category: lastCategory })
    }
  }
  const entries = [...base.entries, ...added]
  let size = entries.reduce((total, entry) => total + entry.text.length, 0)
  let start = 0
  while (start < entries.length && size - entries[start].text.length >= limit) {
    size -= entries[start++].text.length
  }
  const retained = entries.slice(start)
  const omitted = base.omitted || start > 0 || size > limit
  if (size > limit && retained.length)
    retained[0] = { ...retained[0], text: retained[0].text.slice(size - limit) }
  return { entries: retained, lastCategory, omitted }
}

export function consoleOutput(
  buffer: ConsoleBuffer,
  filter: ConsoleFilter,
): string {
  const visible = buffer.entries.filter(
    (entry) =>
      filter === 'traces' ||
      entry.category === 'errors' ||
      (filter === 'events' && entry.category === 'events'),
  )
  const text = visible.map((entry) => entry.text).join('')
  return (
    (buffer.omitted ? '[Earlier output omitted from display]\n' : '') +
    (text || (buffer.entries.length ? 'No output matches this filter' : ''))
  )
}
