# Phase 3 Design — Per-Tenant SSO / Email / Push Config

> **Date:** 2026-04-09
> **Branch:** `feature/multi-tenant`
> **Companion to:** `docs/multi_tenant_roadmap.md` (spec), `roadmap-progess.md` (audit), `docs/superpowers/plans/2026-04-08-phase-2-close-tenant-aware-bg-processes.md` (prior plan template).

---

## Context

**Phase 2 is complete.** 34/0 probe suite. All background loops converted, seed rows seeded, state_group caches tenant-keyed, server_notices sender MXID per-tenant. Pushers were deferred from phase 2 to phase 3.

**Phase 3 unblocks real tenant onboarding.** Without it, registration with email verification, password reset, and IdP login all behave identically for every tenant. Each tenant needs its own OIDC provider, SMTP settings, and push configuration.

### Roadmap scope (§3)

Move `oidc_providers`, `cas_config`, `saml2_config`, `email`, `push`, and Sygnal endpoints into `TenantConfig` (or per-tenant overrides that fall back to global).

---

## Design decisions

### 1. Config inheritance model — Nested config dataclasses (Option B)

Each subsystem gets a per-tenant frozen dataclass on `TenantConfig`. When `None`, the tenant inherits the global config. When set, it **fully replaces** the global config for that subsystem (no field-level merge). This avoids partial-override ambiguity.

**Options considered and rejected:**

| Option | Why rejected |
|---|---|
| A. Flat fields on `TenantConfig` | ~25+ new fields; OIDC provider list awkward as flat field; YAML unwieldy |
| C. Raw dict overrides | No type safety; breaks frozen dataclass pattern; debugging nightmare |

### 2. Decomposition axis — Per-subsystem

- **3a** = SSO (OIDC / CAS / SAML)
- **3b** = Email / SMTP
- **3c** = Push

Each subsystem has its own config shape, handler set, and probe surface. Unlike phase 2 there is no severity gradient, so grouping by functional domain keeps each sub-phase independently shippable and testable.

### 3. Pusher fan-out — Minimal change, no per-tenant loop

`PusherPool` is already user-scoped (pushers belong to a user who pins the tenant). The fix is:

- Make `HttpPusher` / `EmailPusher` resolve push/email config per-invocation from tenant context instead of caching `hs.config.push.*` / `hs.config.email.*` at construction.
- `PusherPool` stays a global singleton; individual pushers resolve tenant config lazily.
- No `run_as_background_process_per_tenant` needed.

### 4. OIDC callback URL — Derived from tenant's `effective_public_baseurl`

`OidcProvider._callback_url` is currently derived from `hs.config.oidc.oidc_callback_url` (which comes from global `public_baseurl`). Under multi-tenant, each `OidcProvider` instance is per-tenant, so the callback URL becomes `{tenant.effective_public_baseurl}_synapse/client/oidc/callback`. nginx already routes by `Host` header, so the IdP redirect lands on the correct tenant domain.

---

## Architecture

### New config dataclasses

All live in `synapse/config/tenants.py` alongside `TenantConfig`.

**`TenantOidcConfig`** — wraps `tuple[OidcProviderConfig, ...]`. Parsed from `oidc_providers` list in tenant YAML block. Reuses the existing `_parse_oidc_provider_configs` machinery from `synapse/config/oidc.py`.

**`TenantCasConfig`** — CAS server URL, service URL, protocol version, displayname attribute, required attributes, registration flag, numeric ID settings, idp name/icon/brand.

**`TenantSamlConfig`** — `Saml2Config` SP config, IDP entity ID, session lifetime, grandfathered MXID source attribute, attribute requirements.

**`TenantEmailConfig`** — SMTP host, port, user, pass, TLS flags (`require_transport_security`, `enable_smtp_tls`, `force_tls`, `tlsname`), `notif_from`, `app_name`, email subjects, `riot_base_url`, `notif_delay_before_mail_ms`.

**`TenantPushConfig`** — `include_content`, `enable_push`, `group_unread_count_by_room`, `jitter_delay_ms`.

Each is added to `TenantConfig` as an `Optional` field defaulting to `None`:

```python
@attr.s(auto_attribs=True, slots=True, frozen=True)
class TenantConfig:
    # ... existing fields ...
    oidc: TenantOidcConfig | None = None
    cas: TenantCasConfig | None = None
    saml: TenantSamlConfig | None = None
    email: TenantEmailConfig | None = None
    push: TenantPushConfig | None = None
```

### Handler conversion pattern

The same pattern used in phases 1-2: replace `__init__`-time config caching with lazy resolution from tenant context at call time.

```python
# Before (phase 2 pattern — cached at init, stale under multi-tenant):
class SomeHandler:
    def __init__(self, hs):
        self._smtp_host = hs.config.email.email_smtp_host

# After (phase 3 pattern — resolved per-request):
class SomeHandler:
    def __init__(self, hs):
        self._global_email_config = hs.config.email  # fallback

    def _get_email_config(self) -> EmailConfig | TenantEmailConfig:
        tenant = get_current_tenant_or_none()
        if tenant and tenant.email:
            return tenant.email
        return self._global_email_config
```

For SSO handlers, the pattern is different because OIDC/CAS/SAML providers are stateful objects (they hold HTTP clients, cached metadata). The `OidcHandler` becomes a dispatcher keyed by `(server_name, idp_id)`.

---

## Sub-phase 3a — SSO (OIDC / CAS / SAML)

### Config layer

- Add `TenantOidcConfig`, `TenantCasConfig`, `TenantSamlConfig` dataclasses.
- Extend `TenantConfig.from_dict` to parse `oidc_providers`, `cas`, `saml` blocks.

### Handler conversion

**`OidcHandler` (`synapse/handlers/oidc.py`):**
- `__init__` (line 127): builds `self._providers` from global config. Under multi-tenant, build a `dict[str, dict[str, OidcProvider]]` keyed by `server_name`. At startup, iterate all tenants with `oidc` config and build their provider sets. Tenants without `oidc` config share the global provider set.
- `handle_oidc_callback` (line 152): tenant context is set by request middleware. Look up providers by `current_tenant.server_name`.
- `load_metadata` (line 137): load metadata for all provider sets (global + per-tenant).

**`OidcProvider` (`synapse/handlers/oidc.py`):**
- `__init__` (line 445): `self._server_name = hs.config.server.server_name` → pass tenant `server_name` at construction time.
- `_callback_url` (line 386): derive from tenant's `effective_public_baseurl`.
- `_callback_path_prefix` (line 390-392): same — use tenant's public_baseurl.

**`CasHandler` (`synapse/handlers/cas.py`):**
- Lines 75-96: 10+ fields cached from `hs.config.cas` → store global config as fallback, resolve from tenant CAS config at each method entry.

**`SamlHandler` (`synapse/handlers/saml.py`):**
- Lines 65-72: 4 fields cached from `hs.config.saml2` → same lazy-resolve pattern. `Saml2Client` construction may need to be per-tenant if tenants have different SP configs.

### Probes

- Two tenants with different OIDC providers → login flow on tenant A hits A's IdP, tenant B hits B's IdP.
- CAS login on tenant A uses A's CAS server URL.
- Tenant with no SSO config inherits global.

---

## Sub-phase 3b — Email / SMTP

### Config layer

- Add `TenantEmailConfig` dataclass.
- Extend `TenantConfig.from_dict` to parse `email` block.

### Handler conversion

**`SendEmailHandler` (`synapse/handlers/send_email.py`):**
- Lines 117-128: 8 SMTP fields cached at init → resolve from tenant email config (or global fallback) at `send_email` call time. The method already receives the recipient, so tenant-context resolution is natural.

**`Mailer` (`synapse/push/mailer.py`):**
- Line 139: `email_subjects` → resolve from tenant config.
- Lines 913-914, 932-934: `email_riot_base_url` → resolve from tenant config.

**`AccountValidityHandler` (`synapse/handlers/account_validity.py`):**
- Lines 48, 69-70: `email_app_name`, templates → resolve from tenant config.

**`PusherFactory` (`synapse/push/pusher.py`):**
- Lines 45-50: `email_enable_notifs` gate → resolve from tenant email config.

### Phase 2c-B deferred server_notices sweep

These reads still use global config and are included in 3b since they share the email/notification domain:

- `synapse/api/auth_blocking.py:40`
- `synapse/handlers/room_member.py:129`
- `synapse/handlers/message.py:848-849`
- `synapse/handlers/register.py:130`
- `synapse/handlers/federation.py:147`
- `synapse/handlers/room.py:191`

Each gets the same treatment: resolve `server_notices_mxid` from tenant context using `effective_server_notices_mxid` instead of `hs.config.servernotices.server_notices_mxid`.

### Probes

- Password reset email for tenant A comes from A's SMTP/from address.
- Tenant with no email config inherits global SMTP settings.
- Server notices reads on non-primary tenant use correct MXID.

---

## Sub-phase 3c — Push

### Config layer

- Add `TenantPushConfig` dataclass.
- Extend `TenantConfig.from_dict` to parse `push` block.

### Handler conversion

**`HttpPusher` (`synapse/push/httppusher.py`):**
- Line 126: `push_group_unread_count_by_room` → resolve from tenant push config.
- Line 130: `push_jitter_delay_ms` → resolve from tenant push config.
- Line 514: `push_include_content` → resolve from tenant push config.

**`EmailPusher` (`synapse/push/emailpusher.py`):**
- Line 86: `notif_delay_before_mail_ms` → resolve from tenant email config.

**`PusherPool` (`synapse/push/pusherpool.py`):**
- Line 73: `self.server_name = hs.hostname` → used for metric labels. Pushers are per-user so tenant context is available when a pusher fires. The pool stays a global singleton.

**`BulkPushRuleEvaluator` (`synapse/push/bulk_push_rule_evaluator.py`):**
- Line 138: `self.should_calculate_push_rules = self.hs.config.push.enable_push` → resolve from tenant push config per evaluation.

### Probes

- Tenant A has push disabled, tenant B has push enabled → notification on A is suppressed, on B is delivered.
- Tenant A has jitter delay, tenant B has none → timing differs.

---

## Config YAML shape

```yaml
multi_tenant:
  enabled: true
  tenants:
    - server_name: acme.com
      signing_key_path: /keys/acme.signing.key
      database_schema: tenant_acme_com
      media_store_path: /data/media/acme.com
      oidc_providers:
        - idp_id: acme-okta
          issuer: https://acme.okta.com/
          client_id: abc
          client_secret: xyz
          scopes: [openid, profile]
          user_mapping_provider:
            config:
              localpart_template: "{{ user.preferred_username }}"
      email:
        smtp_host: smtp.acme.com
        smtp_port: 587
        smtp_user: noreply@acme.com
        smtp_pass: secret
        notif_from: "Acme Chat <noreply@acme.com>"
        force_tls: true
      push:
        include_content: false
        jitter_delay: 5s

    - server_name: corp.com
      signing_key_path: /keys/corp.signing.key
      database_schema: tenant_corp_com
      media_store_path: /data/media/corp.com
      cas:
        server_url: https://cas.corp.com
        service_url: https://corp.com/_matrix/client/r0/login/cas/ticket
      # No email/push blocks → inherits global config
```

---

## Definition of done

1. All probes green (SSO, email, push isolation between tenants).
2. Tenants with no subsystem override inherit global config unchanged.
3. No new `hs.config.*` reads cached at `__init__` time in converted handlers.
4. Phase 2c-B deferred server_notices reads swept.
5. Existing upstream tests pass (`trial tests` green).
6. `roadmap-progess.md` and `multi-tenancy-workflow/data.js` updated via `sync-roadmap`.

---

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| OIDC metadata load at startup for N tenants x M providers | Slow startup | Lazy-load metadata on first request per provider |
| SAML `Saml2Config` from pysaml2 is deeply nested, may not serialize cleanly to per-tenant YAML | Config parsing complexity | If SAML proves too complex, defer to follow-up and document limitation |
| Email template paths — tenants may want custom templates | Scope creep | Phase 3 supports per-tenant SMTP settings only; custom templates are follow-up |
| `SendEmailHandler` is used for both notification emails and admin emails | Tenant context may not be set for admin-initiated emails | Admin emails use global config fallback (no tenant context = global) |
