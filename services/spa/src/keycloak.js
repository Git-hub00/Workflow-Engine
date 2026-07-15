import Keycloak from 'keycloak-js'

const keycloak = new Keycloak({
  url: 'http://localhost:8081',
  realm: 'workflow',
  clientId: 'workflow-spa',
})

export default keycloak
