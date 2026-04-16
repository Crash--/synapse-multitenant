# Phase OIDC — Multi-Tenant Bridge (Design Spec)

**Date:** 2026-04-16
**Branch:** `feature/multi-tenant`
**Status:** Design — to be turned into an implementation plan.
**Companion:**
`docs/superpowers/specs/2026-04-16-docker-demo-lemonldap-sso-design.md`
(the demo stack this wiring unblocks),
`critical-e2e-bugs.md` Pattern A (the repeating dict-vs-registry trap).

---

## Problem

The docker-demo stack now boots with LemonLDAP-NG + OpenLDAP + an OIDC
proxy in front of Synapse, and zero-config provisioning stores a
per-tenant `oidc_config` JSONB blob in `public.tenants`. Despite that,
`GET https://<tenant>.localhost/_matrix/client/v3/login` never
advertises the `m.login.sso` flow — the SSO branch of the login
response is silently dead for every DB-sourced tenant.

Three separate failures conspire:

1. **Data path.** The manager PATCHes `{"enabled": true, "providers": [...]}`
   (envelope) but `TenantOidcConfig.from_list` expects a bare list, so
   `tuple({"enabled": ..., "providers": ...})` yields `("enabled", "providers")` —
   string keys, not provider dicts.
2. **Pattern A.** `OidcHandler._build_tenant_providers` iterates
   `hs.config.multi_tenant.tenants` (the YAML dict, empty in DB-driven
   mode) instead of `hs.get_tenant_registry()`.
3. **Login path.** Even if providers are built, `LoginRestServlet` caches
   `hs.config.oidc.oidc_enabled` at startup — in DB-driven mode that's
   `False` forever, so the SSO flow block at `login.py:144` is
   unreachable. And `SsoHandler.get_identity_providers()` is a flat
   global dict that doesn't filter by tenant.

Additionally: admin reload (`POST /_synapse/admin/v1/tenants/reload`)
currently cascades into the keyring and the app-service / ratelimiter
registries — **not** the OIDC handler. New / updated / removed tenant
OIDC configs don't propagate without a Synapse restart.

A latent fourth issue surfaced while reading the code: every
`OidcProvider.__init__` self-registers with `SsoHandler` via
`register_identity_provider(self)`, which `assert`s that the `idp_id` is
not already registered. Since all tenants in the demo share
`idp_id="lemonldap"`, this asserts out the moment the second tenant is
provisioned. The fix needs to treat tenant-scoped providers as a
distinct registration path.

## Goal

`GET https://<tenant>.localhost/_matrix/client/v3/login` returns the
`m.login.sso` flow with the tenant's LemonLDAP IdP for every DB-sourced
tenant, with no Synapse restart between provisioning and login.
Single-tenant YAML deployments continue to work unchanged.

## Non-goals

- Per-tenant OIDC clients. The demo intentionally shares one client via
  the OIDC proxy; per-tenant clients are a separate design.
- Per-tenant SAML / CAS. The same tenancy pattern should apply later,
  but out-of-scope for this spec.
- UI changes beyond the existing manager drawer. The verification click
  path goes through Element Web.
- Changing the envelope shape the manager PATCHes. The DB-side shape
  stays `{enabled, providers}`; Synapse adapts.

## Design

### Data shape

`public.tenants.oidc_config` is JSONB. Control plane stores:

```json
{
  "enabled": true,
  "providers": [
    { "idp_id": "lemonldap", "idp_name": "LemonLDAP SSO", "issuer": "...", ... }
  ]
}
```

`TenantOidcConfig.from_db_row` must accept this envelope, honour
`enabled: false` (→ treat as no providers), and remain compatible with
the bare-list shape used by YAML tests. A dedicated
`from_db_value(raw)` classmethod handles the polymorphism; `from_list`
stays for the legacy path.

### Tenant provider build

`OidcHandler._build_tenant_providers` walks `hs.get_tenant_registry().get_all_tenants()`.
For each tenant whose `TenantOidcConfig` has providers, it parses them
through `_parse_oidc_provider_configs`, instantiates `OidcProvider`
with `tenant_server_name=` / `tenant_public_baseurl=`, and stores them
under `_tenant_providers[server_name]`.

The iteration tolerates:
- Registry empty at startup (DB-driven mode, tenants arrive via reload).
- Tenants with `TenantOidcConfig.providers == ()` (nothing to build).
- Tenants whose raw config failed to parse (log + skip; don't take
  the whole server down).

### SSO registration — namespaced keys

`OidcProvider.__init__` currently calls
`self._sso_handler.register_identity_provider(self)` unconditionally.
Change: when `tenant_server_name` is set, skip that auto-register and
let `OidcHandler._build_tenant_providers` register explicitly via a
new `SsoHandler.register_tenant_identity_provider(p, tenant_server_name)`.

`SsoHandler` gains a tenant-namespaced key format: the underlying
`_identity_providers` dict stores entries under `<tenant>::<idp_id>`
for tenant-scoped providers and `<idp_id>` for global providers. The
public `get_identity_providers()` method becomes tenant-aware:

- Reads `get_current_tenant()`.
- If a tenant is set, returns `{idp_id: provider}` filtered to entries
  with prefix `<tenant>::` and with the namespace stripped from the
  key. Falls back to global entries when the tenant has none.
- If no tenant context, returns the global subset (entries without a
  `::` in the key).

Existing single-tenant deployments see identical behaviour — the
global providers keep their flat keys, and no tenant context is set.

### Login endpoint — per-request gate

`LoginRestServlet.oidc_enabled` goes away. The SSO flow branch at
`login.py:144` becomes:

```python
if self.cas_enabled or self.saml2_enabled or self._tenant_has_oidc():
    flows.append({
        "type": LoginRestServlet.SSO_TYPE,
        "identity_providers": [...],
    })
```

where `_tenant_has_oidc()` returns `True` if:

- the global `hs.config.oidc.oidc_enabled` is True, OR
- `self._oidc_handler._get_providers()` (the existing
  tenant-context-aware dispatch) returns a non-empty dict.

The `identity_providers` list already iterates
`self._sso_handler.get_identity_providers().values()` — no change
needed there once that method is tenant-aware.

### Reload cascade

Add `OidcHandler.reload(registry)`:

1. Snapshot old `_tenant_providers` keys.
2. Re-run `_build_tenant_providers` against the live registry. Build
   into a fresh dict; don't mutate in place while callers may be
   iterating.
3. Compute adds / removes per tenant.
4. For each removed tenant provider, call a new
   `SsoHandler.deregister_tenant_identity_provider(tenant, idp_id)`.
5. For each added tenant provider, call
   `register_tenant_identity_provider(p, tenant)`.
6. Swap `_tenant_providers` to the new dict.
7. Kick `load_metadata()` for any newly-added providers (best-effort;
   log failures but don't break the reload).

`ReloadTenantsRestServlet` gains one new call in the cascade block
next to `keyring.reload(registry)`:

```python
oidc_handler = self._hs.get_oidc_handler()
if oidc_handler is not None:
    await oidc_handler.reload(registry)
```

The `get_oidc_handler()` accessor may be absent in single-tenant
deployments without OIDC configured — tolerate `None`.

### Why not Option 2 (login endpoint reads OidcHandler directly)

Option 2 avoids the SsoHandler plumbing but couples `LoginRestServlet`
to `OidcHandler` internals and forks the identity-provider discovery
path across SSO types. Future SAML / CAS per-tenant work would either
duplicate the logic or back-fill Option 1 anyway. Option 1 also makes
`get_identity_providers_for_user()` (used by the UIAuth re-auth flow)
tenant-aware "for free" once we filter by ContextVar.

## Testing

### Unit — `tests/tenant/test_oidc_per_tenant.py`

Mock-based Trial tests following the shape of
`tests/tenant/test_federation_inbound.py`:

1. `from_db_row` envelope shape produces the right `TenantOidcConfig`.
2. `from_db_row` honours `enabled: false` → `providers == ()`.
3. `_build_tenant_providers` iterates the registry, not the YAML dict.
4. `SsoHandler.get_identity_providers()` filters by current tenant
   ContextVar.
5. `LoginRestServlet.on_GET` advertises `m.login.sso` for a tenant
   with OIDC configured but none globally.
6. `LoginRestServlet.on_GET` does NOT advertise SSO for a tenant
   without OIDC when none is configured globally.
7. `OidcHandler.reload(registry)` adds newly-provisioned tenants and
   removes deleted ones.

Each sub-phase starts with **red probes** — tests that fail before the
fix and pass after.

### E2E — `docker-demo/scripts/test_tenants.py`

Add a new test step after tenant creation: `curl -sk
https://$TENANT/_matrix/client/v3/login` and assert the response
contains an `m.login.sso` flow with the expected IdP.

### Browser — Playwright (via `playwright-cli` skill)

After the E2E script passes, drive Element Web through the click path:
- Visit `https://acme.localhost/`.
- Assert a "Sign in with LemonLDAP SSO" button is present.
- Click it.
- Assert the redirect lands on `https://lemonldap.localhost/oauth2/authorize?...`.

This is the only browser coverage; the SSO-to-Synapse handshake after
login itself is validated by the existing LemonLDAP stack.

## Risks & landmines

- **Re-introducing Pattern A.** Anyone adding a new read of
  `hs.config.multi_tenant.tenants` re-opens the bug. Code review
  checklist: every tenant iteration must go through
  `hs.get_tenant_registry()`.
- **Non-atomic reload.** A PATCH that adds a tenant AND removes another
  hits `OidcHandler.reload` once; if the add raises, removes must
  still apply (and vice versa). Wrap each add / remove in its own
  try/except; log and continue.
- **Provider `idp_id` collisions across tenants** are the *point* of
  namespacing — don't over-engineer to collapse them.
- **Registration-side collision fix.** `OidcProvider.__init__` must
  skip `_sso_handler.register_identity_provider(self)` when
  `tenant_server_name` is set. Missing this makes provisioning the
  second tenant crash Synapse on startup.
- **UIAuth re-auth path.** `get_identity_providers_for_user()` also
  iterates `_identity_providers`. With namespaced keys, the external
  IDs map stores the bare `idp_id`; look up both `<idp_id>` and any
  `*::<idp_id>` entry, returning whichever matches. Alternative: key
  external IDs by namespaced value going forward; chose the former
  to avoid a data migration.
- **`load_metadata()` failures during reload.** LemonLDAP may not be
  reachable at the exact moment of reload. Log; don't raise. The next
  request triggers lazy retry.
- **Host header forgery.** Tenant selection for the login flow uses
  the same request-path the rest of the fork uses (Host →
  `_tenant_registry.get_tenant_for_host()` → ContextVar). No new
  authentication surface introduced here.

## Decomposition (sub-phases)

- **oidc-a** — `TenantOidcConfig.from_db_row` envelope handling.
- **oidc-b** — `_build_tenant_providers` uses the live registry.
- **oidc-c** — SsoHandler tenancy + `OidcProvider` registration fix.
- **oidc-d** — `LoginRestServlet` per-request SSO gate.
- **oidc-e** — `OidcHandler.reload(registry)` + admin cascade wiring.
- **oidc-f** — E2E + Playwright verification.

Each sub-phase: **probes first** (red), fix, probes green, commit.

## Definition of done

1. Fresh `docker compose up -d` → manager provisions a tenant → drawer
   shows all steps green including `oidc_config`.
2. `curl -sk https://acme.localhost/_matrix/client/v3/login | jq` shows
   `m.login.sso` with the LemonLDAP IdP.
3. Provisioning a second tenant doesn't crash Synapse
   (`register_identity_provider` collision).
4. Element Web "Sign in with LemonLDAP SSO" button present; click
   redirects to LemonLDAP.
5. `alice@acme.localhost` / `demo` logs in successfully end-to-end.
6. Deleting a tenant via the control plane (which triggers reload)
   removes that tenant's IdP entries from `SsoHandler`.
7. Single-tenant YAML deployment `tests.handlers.test_oidc` and
   `tests.rest.client.test_login` remain green.
8. `roadmap-progess.md` updated via `/sync-roadmap`.
