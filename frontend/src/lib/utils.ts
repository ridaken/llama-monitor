import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) { return twMerge(clsx(inputs)) }

export const fmt = (value: number | null | undefined, digits = 0) =>
  value == null ? '—' : Number(value).toFixed(digits)
export const fmtGB = (bytes: number | null | undefined) =>
  bytes == null ? '—' : `${(bytes / 1073741824).toFixed(1)} GB`
export const basename = (path: string) => path.split(/[\\/]/).pop() || ''
export const clamp = (value: number) => Math.min(100, Math.max(0, value))
