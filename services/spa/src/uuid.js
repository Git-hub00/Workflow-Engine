// Secure-context-safe UUID v4.
//
// crypto.randomUUID() is only available in "secure contexts" (HTTPS or
// http://localhost). When the SPA is served over plain HTTP on a bare IP
// (e.g. http://<vm-ip>/), it is undefined and callers throw. crypto.getRandomValues()
// IS available in insecure contexts, so we fall back to a getRandomValues-based v4.
// Prefer the native implementation when present.
export function uuid() {
  const c = globalThis.crypto
  if (c && typeof c.randomUUID === 'function') {
    return c.randomUUID()
  }
  const bytes = new Uint8Array(16)
  c.getRandomValues(bytes)
  bytes[6] = (bytes[6] & 0x0f) | 0x40 // version 4
  bytes[8] = (bytes[8] & 0x3f) | 0x80 // variant 10
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0'))
  return (
    `${hex.slice(0, 4).join('')}-${hex.slice(4, 6).join('')}-` +
    `${hex.slice(6, 8).join('')}-${hex.slice(8, 10).join('')}-${hex.slice(10, 16).join('')}`
  )
}
