import Keycloak from 'keycloak-js'

// Build-time config (Vite `import.meta.env.VITE_*`). The pipeline writes
// services/spa/.env.production so a deployed browser talks to the VM's Keycloak;
// the localhost fallbacks keep `npm run dev` working unchanged.
const keycloak = new Keycloak({
  url: import.meta.env.VITE_KEYCLOAK_URL || 'http://localhost:8081',
  realm: import.meta.env.VITE_KEYCLOAK_REALM || 'workflow',
  clientId: import.meta.env.VITE_KEYCLOAK_CLIENT_ID || 'workflow-spa',
})

export default keycloak
