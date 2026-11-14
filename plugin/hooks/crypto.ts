// HMAC-SHA256 на crypto.subtle.digest: в модах нет crypto.subtle.sign с ключами HMAC.

export const enc = new TextEncoder()

export async function sha256(d: Uint8Array): Promise<Uint8Array> {
  return new Uint8Array(await crypto.subtle.digest('SHA-256', d))
}

export function hex(b: Uint8Array): string {
  return [...b].map(x => x.toString(16).padStart(2, '0')).join('')
}

export async function hmacHex(key: Uint8Array, msg: string): Promise<string> {
  const k = new Uint8Array(64)
  k.set(key.length > 64 ? await sha256(key) : key)
  const m = enc.encode(msg)
  const inner = new Uint8Array(64 + m.length)
  inner.set(k.map(b => b ^ 0x36))
  inner.set(m, 64)
  const outer = new Uint8Array(96)
  outer.set(k.map(b => b ^ 0x5c))
  outer.set(await sha256(inner), 64)
  return hex(await sha256(outer))
}
