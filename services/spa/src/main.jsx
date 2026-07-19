import './polyfills' // MUST be first: installs crypto.randomUUID for HTTP (pre-keycloak)
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.jsx'
import keycloak from './keycloak'
import './index.css'

const rootElement = document.getElementById('root')

keycloak
  .init({
    onLoad: 'login-required',
    // PKCE S256 needs the Web Crypto SubtleCrypto API, which browsers only expose
    // in a secure context (HTTPS or http://localhost). Served over plain HTTP on a
    // bare IP, crypto.subtle is undefined and login fails with "Web Crypto API is
    // not available." Disabled here to match this HTTP demo deployment. Re-enable
    // ('S256') once the app is served over HTTPS.
    pkceMethod: false,
    checkLoginIframe: false,
  })
  .then(() => {
    createRoot(rootElement).render(
      <StrictMode>
        <App />
      </StrictMode>,
    )
  })
  .catch((error) => {
    createRoot(rootElement).render(
      <main className="auth-error">
        <h1>Unable to sign in</h1>
        <p>{error?.message || 'Keycloak initialization failed.'}</p>
      </main>,
    )
  })
