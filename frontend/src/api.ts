function describeDetail(detail: unknown): string | null {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const parts = detail
      .map((item) => (item && typeof item === 'object' && 'msg' in item ? String((item as { msg: unknown }).msg) : JSON.stringify(item)))
      .filter(Boolean)
    return parts.length ? parts.join('; ') : null
  }
  if (detail && typeof detail === 'object') return JSON.stringify(detail)
  return null
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, {
      ...init,
      headers: {
        'Content-Type': 'application/json',
        ...(init?.headers ?? {}),
      },
    })
  } catch {
    // Network-level rejection (offline, server down, CORS) carries a raw
    // TypeError like "Failed to fetch" — translate to an operator message.
    throw new Error('サーバーに接続できません。バックエンドが起動しているか確認してください。')
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: unknown } | null
    throw new Error(describeDetail(payload?.detail) ?? `要求を処理できませんでした (HTTP ${response.status})`)
  }
  return response.json() as Promise<T>
}

export function bytesToBase64(bytes: Uint8Array): string {
  let binary = ''
  const chunkSize = 0x8000
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize))
  }
  return btoa(binary)
}

export async function fileToBase64(file: File): Promise<string> {
  return bytesToBase64(new Uint8Array(await file.arrayBuffer()))
}

export function parseList(text: string): string[] {
  return text.split(/[\n,;]+/).map((value) => value.trim()).filter(Boolean)
}
