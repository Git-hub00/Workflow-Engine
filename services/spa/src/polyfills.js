// Secure-context polyfill — MUST be imported before keycloak-js.
//
// Browsers only expose crypto.randomUUID() and crypto.subtle in a "secure
// context" (HTTPS, or http://localhost). When the SPA is served over plain HTTP
// on a bare IP (http://<vm-ip>/), both are undefined. keycloak-js 26 calls
// crypto.randomUUID() internally (createUUID) to generate the login state/nonce
// on EVERY login — independent of PKCE — so login throws
// "Web Crypto API is not available." even with PKCE disabled.
//
// crypto.getRandomValues() IS available in insecure contexts, so we synthesize a
// spec-shaped v4 UUID from it and install it as crypto.randomUUID when missing.
// This keeps the whole app working over HTTP. It becomes a no-op the moment the
// site is served over HTTPS (native randomUUID present) — safe to keep.
(function installRandomUuidPolyfill() {
  try {
    if (
      typeof crypto !== 'undefined' &&
      typeof crypto.getRandomValues === 'function' &&
      typeof crypto.randomUUID !== 'function'
    ) {
      crypto.randomUUID = function randomUUID() {
        const bytes = new Uint8Array(16)
        crypto.getRandomValues(bytes)
        bytes[6] = (bytes[6] & 0x0f) | 0x40 // version 4
        bytes[8] = (bytes[8] & 0x3f) | 0x80 // variant 10
        const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0'))
        return (
          `${hex.slice(0, 4).join('')}-${hex.slice(4, 6).join('')}-` +
          `${hex.slice(6, 8).join('')}-${hex.slice(8, 10).join('')}-${hex.slice(10, 16).join('')}`
        )
      }
    }
  } catch {
    // If the environment forbids assigning to crypto (already a secure context
    // with a locked property), the native randomUUID is present anyway.
  }
})()
