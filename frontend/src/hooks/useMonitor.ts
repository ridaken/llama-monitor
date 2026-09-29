import { useEffect, useRef, useState } from 'react'
import { api } from '@/api'
import type { Stats } from '@/types'

type Sample = {
  decode: number[]
  sysmem: number[]
  gpu: Record<number, { temp: number[]; util: number[]; power: number[] }>
}
const emptySamples = (): Sample => ({ decode: [], sysmem: [], gpu: {} })
const push = (values: number[], value: number | null | undefined) =>
  [...values, value ?? 0].slice(-60)

export function useMonitor() {
  const [data, setData] = useState<Stats | null>(null)
  const [samples, setSamples] = useState<Sample>(emptySamples)
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null)
  const [failed, setFailed] = useState(false)
  const activeRef = useRef(true)
  const smoothing = useRef<number[]>([])
  useEffect(() => {
    let live = true
    let timer: ReturnType<typeof setTimeout> | undefined
    const controller = new AbortController()
    const poll = async () => {
      let active = false
      try {
        const result = await api<Stats>(
          `/api/stats${activeRef.current ? '' : '?lite=1'}`,
          { signal: controller.signal },
        )
        if (!live) return
        const tp = result.throughput || {}
        active =
          result.active ??
          !!(
            (result.slots?.busy || 0) > 0 ||
            (result.requests?.processing || 0) > 0 ||
            (tp.decode_tps_live || 0) > 0
          )
        if (tp.decode_tps_live == null) smoothing.current = []
        else
          smoothing.current = [...smoothing.current, tp.decode_tps_live].slice(
            -3,
          )
        const smooth = smoothing.current.length
          ? smoothing.current.reduce((a, b) => a + b, 0) /
            smoothing.current.length
          : 0
        setSamples((previous) => {
          const gpu = { ...previous.gpu }
          for (const device of result.gpu?.devices || []) {
            const old = gpu[device.index] || { temp: [], util: [], power: [] }
            gpu[device.index] = {
              temp: push(old.temp, device.temp),
              util: push(old.util, device.util_gpu),
              power: push(old.power, device.power),
            }
          }
          const mem = result.sysmem
          const pct =
            mem?.percent ??
            (mem?.total ? ((mem.used || 0) / mem.total) * 100 : 0)
          return {
            decode:
              tp.decode_tps_live == null
                ? previous.decode
                : push(previous.decode, smooth),
            sysmem:
              mem?.percent == null && !mem?.total
                ? previous.sysmem
                : push(previous.sysmem, pct),
            gpu,
          }
        })
        setData(result)
        setUpdatedAt(new Date())
        setFailed(false)
      } catch (error) {
        if (
          !live ||
          (error instanceof DOMException && error.name === 'AbortError')
        )
          return
        setFailed(true)
      } finally {
        if (live) {
          activeRef.current = active
          timer = setTimeout(poll, active ? 1000 : 3000)
        }
      }
    }
    poll()
    return () => {
      live = false
      clearTimeout(timer)
      controller.abort()
    }
  }, [])
  return { data, samples, updatedAt, failed }
}
