import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.jsx'
import keycloak from './keycloak'
import './index.css'

const rootElement = document.getElementById('root')

keycloak
  .init({
    onLoad: 'login-required',
    pkceMethod: 'S256',
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
