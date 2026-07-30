import keycloak from './keycloak'

// Build-time config. In production the pipeline sets VITE_API_BASE_URL=/api so
// the SPA calls the API same-origin through nginx (no CORS). Dev falls back to
// the local API on :8000.
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'

async function request(path, { method = 'GET', body } = {}) {
  await keycloak.updateToken(30)

  const headers = new Headers({
    Authorization: `Bearer ${keycloak.token}`,
  })

  const options = { method, headers }
  if (body !== undefined) {
    headers.set('Content-Type', 'application/json')
    options.body = JSON.stringify(body)
  }

  const response = await fetch(`${API_BASE_URL}${path}`, options)
  const responseText = await response.text()

  if (!response.ok) {
    const error = new Error(responseText || `${response.status} ${response.statusText}`)
    error.status = response.status
    throw error
  }

  if (!responseText) return null

  try {
    return JSON.parse(responseText)
  } catch {
    return responseText
  }
}

export const get = (path) => request(path)
export const post = (path, body) => request(path, { method: 'POST', body })
export const put = (path, body) => request(path, { method: 'PUT', body })
export const del = (path) => request(path, { method: 'DELETE' })
