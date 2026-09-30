export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, options)
  let data: unknown
  try {
    data = await response.json()
  } catch {
    data = null
  }
  if (!response.ok) {
    const message =
      data && typeof data === 'object' && 'error' in data
        ? String(data.error)
        : `HTTP ${response.status}`
    throw new Error(message)
  }
  return data as T
}

export const post = <T>(path: string, body: unknown) =>
  api<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
