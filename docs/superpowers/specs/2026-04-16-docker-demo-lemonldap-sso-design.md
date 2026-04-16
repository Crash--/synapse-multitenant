# Docker Demo — LemonLDAP + LDAP SSO Design

**Date:** 2026-04-16
**Scope:** `docker-demo/` stack only. Adds SSO + directory to the zero-config provisioning work from 2026-04-15.
**Reference:** `/home/monta/Desktop/stg/twake-chat-control-plane` + `/home/monta/Desktop/stg/twake-chat-oidc-proxy`
**Prerequisite:** 2026-04-15 zero-config provisioning spec is shipped (manager UI, control plane SSE, Traefik wildcard, dnsmasq).

---

## 1. Problem Statement

Users on the demo today register directly against Synapse with a password. To demo multi-tenant SSO properly we want:

- A directory (OpenLDAP) with per-tenant branches and pre-seeded users.
- An SSO portal (LemonLDAP-NG) that terminates the OIDC flow with Synapse.
- A thin OIDC proxy between Synapse and LemonLDAP that presents a single shared OIDC client to all tenants — matching the architecture Twake already built in production.
- Zero manual config per tenant: creating `tenant-z` in the manager UI also creates its LDAP branch, seeds `alice/bob/charlie`, and wires up Synapse's per-tenant OIDC config (phase 3's `TenantOidcConfig` is already in place).

Goal: after clicking "Create tenant" in the manager, within ~15 seconds you can click "Sign in with SSO" on `https://tenant-z.localhost/`, enter `alice@tenant-z.localhost` + `demo`, and land in Element as `@alice:tenant-z.localhost`.

## 2. Design Decisions

### 2.1 One shared OIDC client via proxy (not per-tenant clients)

Matches the Twake pattern: one `synapse-demo` client in LemonLDAP, used by every tenant. Tenant identity multiplexes via the OIDC `state` parameter (opaque to LemonLDAP, encoded by our proxy). Proxy validates email domain at token exchange to block cross-tenant logins.

**Rejected:** per-tenant LemonLDAP OIDC clients. Would require dynamic client registration at tenant-create time (LemonLDAP Manager REST API). Adds a service, adds complexity, doesn't materially improve isolation beyond what the domain-validation check already gives us.

### 2.2 b2b LDAP tree with tenant-qualified email logins

```
dc=demo,dc=local
└── ou=organizations
    ├── o=acme
    │   └── ou=users
    │       ├── uid=alice (mail: alice@acme.localhost)
    │       ├── uid=bob   (mail: bob@acme.localhost)
    │       └── uid=charlie
    ├── o=corp
    │   └── ou=users
    │       ├── uid=alice (mail: alice@corp.localhost)   -- same uid, different mail
    │       └── ...
    └── o=<tenant>/...
```

- `uid` is NOT globally unique (alice can exist in multiple tenants).
- `mail` IS globally unique — tenant-qualified (`<localpart>@<tenant>.localhost`).
- LemonLDAP login filter: `(mail=%s)` — search entire tree by email.

**Rejected:** flat user tree with `workplaceFqdn` attribute (what ToM-server does). The branch-per-tenant shape is what the user asked for and is cleaner for demo purposes (you can browse the LDAP tree in a client and see the structure).

### 2.3 Dummy-user defaults, manager-driven provisioning

Every tenant auto-gets `alice`, `bob`, `charlie` with password `demo`. Manager UI streams this as extra steps in the provisioning drawer after the control plane finishes its work. No user input.

### 2.4 Manager handles demo-only wiring; control plane stays production-realistic

| Layer | New responsibilities in this spec |
|---|---|
| Control plane | Unchanged from 2026-04-15 spec. Still: schema, keys, activate, reload. Tenant row gains a populated `oidc_config` column (already exists on the table) when manager calls `PATCH /tenants/:name`. |
| Manager | New SSE steps: create LDAP branch + seed users via LDAP REST, then PATCH tenant's `oidc_config` via control plane API. |
| Proxy | New standalone service. No per-tenant state. One env var for the LemonLDAP upstream. |
| LemonLDAP | Single static config (baked `lmConf-1.json`). ONE OIDC client. Points at OpenLDAP as auth backend. |
| OpenLDAP | Seeded once at boot with `dc=demo,dc=local` + `ou=organizations` (no tenants yet). |

## 3. Architecture

### 3.1 New compose services

```
openldap         — OpenLDAP 2.6 (osixia/openldap:1.5.0)
lemonldap        — yadd/lemonldap-ng-portal:latest (portal only; manager not needed at runtime)
oidc-proxy       — our fork of twake-chat-oidc-proxy, adapted for .localhost + .local routing
```

No `ldap-seeder` service — OpenLDAP's image runs LDIF bootstrap itself from a volume mount, so a one-shot job isn't needed.

### 3.2 Static hostnames (via Traefik wildcard route)

All routed by the existing wildcard Traefik rule:

- `https://lemonldap.localhost` — LemonLDAP portal (login UI)
- `https://oidc-proxy.localhost` — OIDC proxy (what Synapse talks to)
- `https://ldap.localhost` — optional, LDAP browser (slapd doesn't really need HTTPS but we expose stats endpoints)

Cert script (`setup_certs.sh`) pre-bakes these into the SAN list.

### 3.3 LemonLDAP configuration (static)

`docker-demo/lemonldap/lmConf-1.json` — committed, immutable:

- Domain: `.localhost`
- Portal URL: `https://lemonldap.localhost/`
- LDAP backend: `ldap://openldap:389` with `ldapBase: dc=demo,dc=local`, `ldapFilter: "(mail=$login)"`
- OIDC issuer active, issuer path `/oauth2/`
- Exported vars from LDAP: `uid`, `cn`, `mail`
- One OIDC Relying Party: `synapse-demo`
  - `client_id: synapse-demo`
  - `client_secret: demo-secret-change-in-prod`
  - Allowed redirect URI: `https://oidc-proxy.localhost/api/oidc/callback` (proxy's callback only — all tenants funnel through this)
  - Exported claims: `sub=mail`, `email=mail`, `preferred_username=uid`

Pre-generated RSA signing keys baked in (copy from Twake's lmConf-1.json reference).

### 3.4 OIDC proxy

Fork of `twake-chat-oidc-proxy` under `docker-demo/oidc-proxy/`. Changes:

- Config: `OIDC_ISSUER_URL=https://lemonldap.localhost/`, `PROXY_URL=https://oidc-proxy.localhost`, `OIDC_CLIENT_ID=synapse-demo`, `OIDC_CLIENT_SECRET=demo-secret-change-in-prod`.
- **Enable the domain-validation check** that's disabled in upstream (the `&& 0` in `token.ts`). This is what prevents cross-tenant logins. Rule: email domain must match the `redirect_uri` host. Example: `alice@acme.localhost` with `redirect_uri=https://acme.localhost/_synapse/client/oidc/callback` → OK. `alice@acme.localhost` with `redirect_uri=https://corp.localhost/...` → 403.
- TLS trust: accept mkcert-signed certs for `*.localhost`. In dev we set `NODE_TLS_REJECT_UNAUTHORIZED=0` (demo only — same pattern as Synapse's `federation_verify_certificates: false`).
- Listen on `3002` internal; Traefik routes `oidc-proxy.localhost` to it.

### 3.5 OpenLDAP seeding

Mounted LDIFs in `docker-demo/openldap/bootstrap/`:

1. `01-base.ldif` — creates `dc=demo,dc=local` + `ou=organizations,dc=demo,dc=local`. Sets admin DN `cn=admin,dc=demo,dc=local` with password `admin`.
2. `02-acl.ldif` — allows authenticated LDAP admin to read/write the tree; anonymous bind allowed for `uid` attribute only (LemonLDAP needs this for binds).

Per-tenant branches (`o=acme,…`) are NOT in the bootstrap — they're created dynamically by the manager.

### 3.6 Synapse per-tenant OIDC config

Stored in `public.tenants.oidc_config` (JSONB column — already present from phase 7). Written by the manager via `PATCH /api/v1/tenants/:name`.

Schema for each tenant's `oidc_config`:

```json
{
  "enabled": true,
  "providers": [
    {
      "idp_id": "lemonldap",
      "idp_name": "LemonLDAP SSO",
      "discover": false,
      "issuer": "https://lemonldap.localhost/",
      "authorization_endpoint": "https://oidc-proxy.localhost/api/oidc/authorize",
      "token_endpoint": "https://oidc-proxy.localhost/api/oidc/token",
      "userinfo_endpoint": "https://oidc-proxy.localhost/api/oidc/userinfo",
      "jwks_uri": "https://oidc-proxy.localhost/api/oidc/jwks",
      "client_id": "synapse-demo",
      "client_secret": "demo-secret-change-in-prod",
      "client_auth_method": "client_secret_basic",
      "scopes": ["openid", "email", "profile"],
      "skip_verification": true,
      "allow_existing_users": true,
      "enable_registration": true,
      "user_mapping_provider": {
        "config": {
          "subject_claim": "sub",
          "localpart_template": "{{ user.sub.split('@')[0] }}",
          "display_name_template": "{{ user.sub.split('@')[0] }}",
          "email_template": "{{ user.sub }}"
        }
      }
    }
  ]
}
```

The per-tenant block is **identical** across tenants (shared OIDC client). The per-tenant-ness comes entirely from the `redirect_uri` being `https://<tenant>.localhost/_synapse/client/oidc/callback` — Synapse derives this from `public_baseurl` at request time. Tenant context (set by Host header middleware) ensures the right tenant's `oidc_config` is loaded and the right `public_baseurl` is used.

### 3.7 Manager: new SSE steps

The provisioning drawer currently streams `keygen → db_row → schema_clone → media_dir → activate → synapse_reload → complete`. After `synapse_reload` and BEFORE `complete`, add:

- `ldap_branch` — manager calls OpenLDAP via `ldapjs` (node lib), creates `o=<tenant>,ou=organizations,dc=demo,dc=local` + `ou=users,o=<tenant>,...`.
- `seed_users` — creates 3 `inetOrgPerson` entries `alice`, `bob`, `charlie` under `ou=users`. Password `demo` (SSHA-hashed via `slappasswd` shelled out — or hash in Node, either works).
- `oidc_config` — manager calls `PATCH /api/v1/tenants/:name` on the control plane with the JSON from §3.6. Control plane writes `oidc_config` column and triggers another Synapse reload.

On failure at any step: the drawer shows the failed step in red. Retry = delete tenant, re-create.

### 3.8 User UX — login flow walk-through

```
1. Browser: https://acme.localhost/
   → Element Web loads from the Synapse host (or from a separate Element deploy — for demo we hit Matrix directly via the manager's quick-link)

2. Element: POST /_matrix/client/v3/login  → 200 with flows including
   { "type": "m.login.sso", "identity_providers": [ { "id": "lemonldap", ... } ] }

3. User clicks "Sign in with LemonLDAP SSO" →
   GET https://acme.localhost/_matrix/client/v3/login/sso/redirect?redirectUrl=…

4. Synapse picks tenant from Host header (acme), reads tenant's oidc_config,
   builds authorize URL:
   → 302 to https://oidc-proxy.localhost/api/oidc/authorize
       ?client_id=synapse-demo
       &redirect_uri=https://acme.localhost/_synapse/client/oidc/callback
       &state=<synapse-state>
       &scope=openid email profile

5. OIDC proxy encodes Synapse's redirect_uri + state into base64 JSON state,
   → 302 to https://lemonldap.localhost/oauth2/authorize
       ?client_id=synapse-demo
       &redirect_uri=https://oidc-proxy.localhost/api/oidc/callback
       &state=<encoded>
       &scope=openid email profile

6. LemonLDAP portal: login form. User enters:
   email:    alice@acme.localhost
   password: demo

7. LemonLDAP LDAP bind with filter (mail=alice@acme.localhost) →
   finds uid=alice,ou=users,o=acme,ou=organizations,dc=demo,dc=local
   with password SSHA match.

8. LemonLDAP → 302 back to proxy with ?code=…&state=<encoded>

9. Proxy decodes state, stores code→redirect_uri mapping,
   → 302 to https://acme.localhost/_synapse/client/oidc/callback?code=…&state=<synapse-state>

10. Synapse (tenant ctx = acme) POST to oidc-proxy/api/oidc/token.
    Proxy exchanges code with LemonLDAP, gets id_token with sub=alice@acme.localhost.
    DOMAIN VALIDATION: email domain "acme.localhost" matches callback host "acme.localhost" → pass.
    Proxy returns LemonLDAP's tokens verbatim to Synapse.

11. Synapse reads userinfo → sub=alice@acme.localhost → localpart_template yields "alice".
    Tenant ctx = acme.localhost → creates @alice:acme.localhost.
    Redirect to Element with access token → user is in.
```

### 3.9 Cross-tenant login blocking

Attacker scenario: alice's acme credentials, attempt to use them on corp. They click "Sign in with SSO" on `corp.localhost`. Steps 1–7 succeed (LemonLDAP doesn't know/care about tenant). At step 10:
- `sub=alice@acme.localhost`
- callback host in redirect_uri = `corp.localhost`
- domain validation fails → proxy returns 403 → Synapse shows login error.

This is the critical check that makes the one-shared-client design safe.

## 4. Testing

### 4.1 Unit tests

- **Proxy** (`docker-demo/oidc-proxy/src/**/*.test.ts`): existing upstream tests + a new `domain-validation.test.ts` that exercises cross-tenant attempts and expects 403.
- **Manager** (`docker-demo/manager/`): no new unit tests — all the interesting logic is side-effectful LDAP/HTTP calls, covered by e2e.
- **Control plane**: no changes to its interface. Existing tests still pass.

### 4.2 End-to-end (docker-demo/scripts/test_tenants.py)

New test functions:

- `test_ldap_branch_exists` — after tenant creation, `ldapsearch -b 'o=<tenant>,ou=organizations,dc=demo,dc=local'` returns 1 org + 3 users.
- `test_lemonldap_portal_reachable` — GET `https://lemonldap.localhost/` returns 200 with a login form in the HTML.
- `test_sso_login_full_flow` — headless: use Python `requests` to simulate the OIDC flow end-to-end for `alice@acme.localhost`. Assert final response has `access_token` and user_id `@alice:acme.localhost`.
- `test_sso_cross_tenant_rejected` — attempt to log in `alice@acme.localhost` with redirect_uri pointing at `corp.localhost`, assert 403 from proxy.

### 4.3 Manual verification

- Clean up: `docker compose down -v && ./scripts/setup_certs.sh && docker compose up -d`.
- Create tenant via manager UI.
- Drawer shows new `ldap_branch`, `seed_users`, `oidc_config` steps all green.
- Visit `https://<tenant>.localhost/` — see "Sign in with LemonLDAP SSO" option.
- Click → enter `alice@<tenant>.localhost` + `demo` → land in Element.
- Repeat for a second tenant with `bob`. Invite cross-tenant, send a message (exercises phases 9/10/10').

## 5. Compose topology after changes

```
services:
  openldap          # NEW
  lemonldap         # NEW (portal only, no manager service)
  oidc-proxy        # NEW (TypeScript Fastify, built from ./oidc-proxy/)
  dnsmasq           # unchanged
  traefik           # unchanged (wildcard catches the 3 new hostnames)
  postgres          # unchanged
  pgbouncer         # unchanged
  synapse           # changed — oidc_config consulted at login
  control-plane     # unchanged
  manager           # changed — 3 new provisioning steps, new env for LDAP + control-plane creds
```

## 6. Definition of Done

- [ ] `./scripts/setup_certs.sh && docker compose up -d` brings everything up healthy.
- [ ] Cert covers `lemonldap.localhost`, `oidc-proxy.localhost`, `ldap.localhost` in SANs.
- [ ] Creating a tenant through the manager drawer shows `ldap_branch → seed_users → oidc_config` steps, all green within ~10 seconds.
- [ ] `https://<tenant>.localhost/_matrix/client/v3/login` response includes an SSO IdP `lemonldap`.
- [ ] Full SSO login for `alice@<tenant>.localhost` succeeds, Matrix user is `@alice:<tenant>.localhost`.
- [ ] Cross-tenant login attempt (alice@acme on corp) returns 403 from proxy.
- [ ] All e2e tests in `scripts/test_tenants.py` green.

## 7. Risks and mitigations

| Risk | Mitigation |
|---|---|
| LemonLDAP's baked config becomes stale as the upstream image evolves | Pin image tag; re-test on upgrade. |
| mkcert root CA not trusted inside the proxy container | `NODE_TLS_REJECT_UNAUTHORIZED=0` in the demo-only proxy compose. Documented. |
| OpenLDAP LDIF bootstrap runs only on empty-volume first-boot; rebooting with stale state skips it | Document `docker compose down -v` as the reset-to-clean command in README. |
| Dummy password `demo` reused across all users | Explicit README warning; acceptable for demo. |
| Manager storing the OIDC client_secret in the tenant row | It's already `demo-secret-change-in-prod`; we're not trying to hide it. Real deployments would use a secrets manager. |
| If a user renames `tenant-a → tenant-aa`, existing user mail attributes go stale | Out of scope — renames aren't supported. Delete + recreate. |

## 8. Out of scope

- Per-tenant LemonLDAP OIDC clients (deferred — shared client with proxy validation is sufficient).
- Dynamic OIDC client registration via LemonLDAP Manager REST API (not needed with shared client).
- User lifecycle beyond seeding (no delete/rename/password-reset UX in manager).
- MFA, consent screens, profile editing — LemonLDAP has these as features but they add UX we don't need.
- Element Web deployment — demo hits Matrix endpoints directly (or user runs Element Web locally against `https://<tenant>.localhost`).
- LDAP replication, TLS for LDAP (ldaps://), password policies, account lockout.
- Tests in plain `trial` for OIDC stuff — our OIDC fixes live outside Synapse's test surface.
