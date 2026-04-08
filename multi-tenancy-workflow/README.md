# Multi-Tenancy Workflow — Animated Explainer

A self-contained animated HTML page explaining how multi-tenancy is
implemented in this Synapse fork. Designed for developer onboarding.

## What it shows

A `GET /_matrix/client/versions` request with `Host: acme.localhost`
flowing through the request lifecycle:

1. Client → Nginx (Host header preserved)
2. Synapse `TenantRouter` resolves the tenant
3. `TenantConfig` loaded and bound to a `contextvars.ContextVar`
4. Postgres `search_path` switched to the tenant's schema
5. `MultiTenantKeyring` returns the tenant's signing key
6. Media path resolves under the tenant's directory
7. Response leaves tagged with the tenant

Three synchronized scenes (switchable via tabs) show this same lifecycle
from different angles: a flow diagram, a layered stack, and a
diagram-plus-log split screen.

## Running

Requires Node.js ≥ 18. No dependencies to install.

    npm start
    # → http://127.0.0.1:8765/

Set `PORT` or `HOST` to override:

    PORT=3000 npm start

You can also just open `index.html` directly via `file://`, but some
browsers block ES module imports from the local filesystem — running
the server avoids that.

## Keyboard

- Space — play / pause
- ← / → — previous / next stage
- 1 / 2 / 3 — switch scene
- R — restart
