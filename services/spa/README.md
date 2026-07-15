# Workflow Engine SPA

React/Vite frontend for the invoice-approval workflow engine.

## Prerequisites

- Node.js and npm
- Keycloak at `http://localhost:8081`, realm `workflow`, public client `workflow-spa`
- API at `http://localhost:8000`
- The workflow worker and Docker services (Temporal, PostgreSQL, Ollama, and Keycloak)

## Run

```powershell
cd services\spa
npm run dev
```

Open `http://localhost:5173`. Keycloak redirects unauthenticated users to login.

## Tabs

- **Task Inbox** — lists open tasks, joins invoice snapshots with audit history, shows the latest AI rationale, and supports role-gated claim/complete actions.
- **Monitor** — polls recent transactions, summarizes statuses, and drills into ordered audit events.
- **Configuration** — reads and updates the live `invoice_approval` config. Changes apply to new transactions.
- **Process Flow** — renders an SVG invoice decision flow using the live thresholds, required fields, and quorum.
