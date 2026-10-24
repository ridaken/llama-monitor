import { useRef, useState } from 'react'
import { FlaskConical } from 'lucide-react'
import { post } from '@/api'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import type { TestPromptResult } from '@/types'

const prompt =
  'Write a concise Python 3 program that prints FizzBuzz for integers 1 through 100. Output only the code.'

export function TestPrompt({ disabled }: { disabled: boolean }) {
  const [open, setOpen] = useState(false)
  const [pending, setPending] = useState(false)
  const [result, setResult] = useState<TestPromptResult | null>(null)
  const [error, setError] = useState('')
  const sending = useRef(false)
  const send = async () => {
    if (disabled || sending.current) return
    sending.current = true
    setPending(true)
    setOpen(true)
    setResult(null)
    setError('')
    try {
      setResult(await post<TestPromptResult>('/api/inference/test', {}))
    } catch (err) {
      setError((err as Error).message)
    } finally {
      sending.current = false
      setPending(false)
    }
  }
  const reasoning =
    result?.message.reasoning_content || result?.message.reasoning
  return (
    <>
      <Button
        id="test-prompt"
        variant="outline"
        disabled={disabled || pending}
        onClick={() => void send()}
      >
        <FlaskConical />
        {pending ? 'Testing…' : 'Test prompt'}
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-h-[85dvh] min-w-0 overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>Model test</DialogTitle>
            <DialogDescription>
              A fixed FizzBuzz request using your current inference route.
            </DialogDescription>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">{prompt}</p>
          {pending && (
            <p role="status" className="text-sm">
              Sending test prompt… Waiting for the complete response (up to 120
              seconds).
            </p>
          )}
          {error && (
            <p role="alert" className="break-words text-sm text-red-300">
              {error}
            </p>
          )}
          {result && (
            <>
              <p role="status" className="break-words text-sm">
                {result.route === 'proxy' ? 'Via proxy' : 'Direct'} ·{' '}
                {result.elapsed_seconds.toFixed(1)}s
                {result.model && ` · ${result.model}`}
              </p>
              {result.finish_reason === 'length' && (
                <p className="text-sm text-amber-300">
                  The generation limit was reached. Output may be incomplete.
                </p>
              )}
              {result.message.content?.trim() ? (
                <pre
                  aria-label="Model reply"
                  className="max-h-80 min-w-0 overflow-auto whitespace-pre-wrap break-all rounded-lg border bg-muted/30 p-3 text-xs"
                >
                  {result.message.content}
                </pre>
              ) : (
                <p className="text-sm text-amber-300">
                  The model returned reasoning without a final answer.
                </p>
              )}
              {reasoning && (
                <details className="min-w-0 text-sm">
                  <summary className="cursor-pointer">Reasoning</summary>
                  <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-all text-xs">
                    {reasoning}
                  </pre>
                </details>
              )}
              {result.request_id && (
                <p className="break-all text-xs text-muted-foreground">
                  Request ID: {result.request_id} · View in Requests
                </p>
              )}
            </>
          )}
        </DialogContent>
      </Dialog>
    </>
  )
}
