# Phase OIDC — Multi-Tenant Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `GET /_matrix/client/v3/login` advertise `m.login.sso` per tenant in DB-driven mode, driven entirely from the DB-loaded `oidc_config`, with live reload and no restart.

**Architecture:** DB JSONB `{enabled, providers}` → `TenantOidcConfig` → `OidcHandler._tenant_providers` (built from the live registry, not the YAML dict) → `SsoHandler._identity_providers` keyed `<tenant>::<idp_id>` → `LoginRestServlet` per-request gate. Reload cascades via `ReloadTenantsRestServlet`.

**Tech Stack:** Synapse (Python / Twisted / Trial), `attrs` dataclasses, existing `TenantRegistry` / `tenant_context` ContextVar, control-plane Fastify (unchanged here), `docker-demo/scripts/test_tenants.py` for E2E, `playwright-cli` skill for the browser smoke.

**Spec:** `docs/superpowers/specs/2026-04-16-phase-oidc-multitenant-bridge-design.md`

**Conventions (read once):**
- Run tests with `SYNAPSE_SKIP_RUST_CHECK=1 trial <dotted.path>` (see `CLAUDE.md`).
- Never read `hs.config.multi_tenant.tenants` for tenant iteration. Always `hs.get_tenant_registry().get_all_tenants()`. This is Pattern A from `critical-e2e-bugs.md`.
- Probes-first per sub-phase: red test first, then fix, then green.
- After each sub-phase ships (green + commit), **run `/sync-roadmap`**.

---

## File Structure

Files touched by this plan:

| Path | Change | Responsibility |
|---|---|---|
| `synapse/config/tenants.py` | Modify `TenantOidcConfig` | Add `from_db_value` polymorphic parser; adjust `from_db_row` call site. |
| `synapse/handlers/oidc.py` | Modify `OidcHandler._build_tenant_providers`, `OidcProvider.__init__` | Iterate live registry; skip auto-register when tenant-scoped; add `OidcHandler.reload(registry)`. |
| `synapse/handlers/sso.py` | Modify `SsoHandler` | Add `register_tenant_identity_provider` / `deregister_tenant_identity_provider`; make `get_identity_providers()` tenant-aware; adjust `get_identity_providers_for_user`. |
| `synapse/rest/client/login.py` | Modify `LoginRestServlet` | Replace cached `oidc_enabled` with per-request `_tenant_has_oidc()`. |
| `synapse/rest/admin/tenants.py` | Modify `ReloadTenantsRestServlet` | Cascade to `oidc_handler.reload(registry)`. |
| `tests/tenant/test_oidc_per_tenant.py` | Create | Mock-based Trial probes for the whole chain. |
| `docker-demo/scripts/test_tenants.py` | Modify | Assert `m.login.sso` flow present post-provision. |

---

## Task 1 — oidc-a: Envelope parsing in `TenantOidcConfig.from_db_row`

**Sub-phase goal:** DB-stored `{enabled, providers}` envelope parses into a `TenantOidcConfig` whose `providers` tuple contains the provider dicts. `{enabled: false}` yields an empty tuple. Bare-list back-compat preserved.

**Files:**
- Modify: `synapse/config/tenants.py:77-86` (`TenantOidcConfig`), `synapse/config/tenants.py:487-488` (`from_db_row` call site)
- Test: `tests/tenant/test_oidc_per_tenant.py`

- [ ] **Step 1: Create the probe file with envelope / bare-list / disabled tests**

Create `tests/tenant/test_oidc_per_tenant.py`:

```python
#
# Copyright 2026 Linagora
#
"""Red/green probes for phase OIDC — per-tenant SSO login bridge."""

from unittest import TestCase

from synapse.config.tenants import TenantOidcConfig


class TestTenantOidcConfigFromDbValue(TestCase):
    """from_db_value must accept both envelope and bare-list shapes."""

    _PROVIDER = {
        "idp_id": "lemonldap",
        "idp_name": "LemonLDAP SSO",
        "issuer": "https://lemonldap.localhost/",
        "client_id": "synapse-demo",
        "client_secret": "demo-secret-change-in-prod",
        "scopes": ["openid", "profile", "email"],
    }

    def test_envelope_enabled(self) -> None:
        """Envelope {'enabled': True, 'providers': [...]} parses to a
        TenantOidcConfig whose providers tuple contains the provider dict."""
        raw = {"enabled": True, "providers": [self._PROVIDER]}
        cfg = TenantOidcConfig.from_db_value(raw)
        self.assertEqual(len(cfg.providers), 1)
        self.assertEqual(cfg.providers[0]["idp_id"], "lemonldap")

    def test_envelope_disabled(self) -> None:
        """Envelope with enabled=False yields an empty tuple."""
        raw = {"enabled": False, "providers": [self._PROVIDER]}
        cfg = TenantOidcConfig.from_db_value(raw)
        self.assertEqual(cfg.providers, ())

    def test_bare_list_backwards_compatible(self) -> None:
        """A bare list of provider dicts still works (legacy shape)."""
        raw = [self._PROVIDER]
        cfg = TenantOidcConfig.from_db_value(raw)
        self.assertEqual(len(cfg.providers), 1)
        self.assertEqual(cfg.providers[0]["idp_id"], "lemonldap")

    def test_envelope_missing_providers_key(self) -> None:
        """Envelope without 'providers' key yields empty tuple."""
        raw = {"enabled": True}
        cfg = TenantOidcConfig.from_db_value(raw)
        self.assertEqual(cfg.providers, ())
```

- [ ] **Step 2: Run the probes to confirm they fail**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 trial tests.tenant.test_oidc_per_tenant.TestTenantOidcConfigFromDbValue 2>&1 | tail -20
```

Expected: 4 failures/errors — `TenantOidcConfig` has no `from_db_value`.

- [ ] **Step 3: Implement `from_db_value` on `TenantOidcConfig`**

In `synapse/config/tenants.py`, replace the class body (lines 77-86) with:

```python
@attr.s(auto_attribs=True, slots=True, frozen=True)
class TenantOidcConfig:
    """Per-tenant OIDC configuration.
    Stores raw provider dicts that get parsed by _parse_oidc_provider_configs
    at handler init time."""
    providers: tuple = attr.Factory(tuple)

    @classmethod
    def from_list(cls, providers: list) -> "TenantOidcConfig":
        """Legacy bare-list parser (retained for YAML / tests)."""
        return cls(providers=tuple(providers))

    @classmethod
    def from_db_value(cls, raw: object) -> "TenantOidcConfig":
        """Parse the DB JSONB ``oidc_config`` column.

        Accepts either:
          * a bare list of provider dicts (legacy), or
          * the envelope ``{"enabled": bool, "providers": [...]}``
            written by the control plane.

        ``enabled: false`` yields an empty providers tuple — the tenant
        has no OIDC providers even if the list is non-empty.
        """
        if isinstance(raw, dict):
            if not raw.get("enabled", True):
                return cls(providers=())
            providers = raw.get("providers", []) or []
            return cls(providers=tuple(providers))
        if isinstance(raw, list):
            return cls(providers=tuple(raw))
        return cls(providers=())
```

- [ ] **Step 4: Switch the `from_db_row` call site to `from_db_value`**

In `synapse/config/tenants.py:487-488`, replace:

```python
        oidc_raw = row.get("oidc_config")
        oidc_cfg = TenantOidcConfig.from_list(oidc_raw) if oidc_raw else None
```

with:

```python
        oidc_raw = row.get("oidc_config")
        oidc_cfg = TenantOidcConfig.from_db_value(oidc_raw) if oidc_raw else None
```

- [ ] **Step 5: Run probes — expect green**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 trial tests.tenant.test_oidc_per_tenant.TestTenantOidcConfigFromDbValue 2>&1 | tail -10
```

Expected: 4/4 pass.

- [ ] **Step 6: Regression check — existing tests untouched**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 trial tests.tenant 2>&1 | tail -10
```

Expected: no new failures vs. baseline.

- [ ] **Step 7: Commit**

```bash
git add synapse/config/tenants.py tests/tenant/test_oidc_per_tenant.py
git commit -m "$(cat <<'EOF'
feat(oidc-a): accept envelope shape in TenantOidcConfig DB parser

Control plane PATCHes oidc_config as {"enabled": bool, "providers": [...]}
but from_list treated it as a bare list — tuple() over the dict yielded
string keys. Add TenantOidcConfig.from_db_value polymorphic parser that
handles both the envelope (honouring enabled=false) and the legacy bare
list, and point from_db_row at it.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2 — oidc-b: `_build_tenant_providers` iterates the live registry

**Sub-phase goal:** `OidcHandler._build_tenant_providers` reads tenants from `hs.get_tenant_registry().get_all_tenants()` — not the YAML dict. Pattern A fix.

**Files:**
- Modify: `synapse/handlers/oidc.py:143-165`
- Test: `tests/tenant/test_oidc_per_tenant.py`

- [ ] **Step 1: Add red probe — `_build_tenant_providers` must read the registry**

Append to `tests/tenant/test_oidc_per_tenant.py`:

```python
from unittest.mock import MagicMock
from synapse.config.tenants import TenantConfig


def _make_tenant_with_oidc(
    server_name: str,
    provider: dict,
) -> TenantConfig:
    return TenantConfig(
        server_name=server_name,
        database_schema=f"tenant_{server_name.replace('.', '_')}",
        signing_key_path=f"/keys/{server_name}.key",
        media_store_path=f"/media/{server_name}",
        oidc=TenantOidcConfig(providers=(provider,)),
    )


class TestBuildTenantProvidersReadsRegistry(TestCase):
    """_build_tenant_providers must iterate the live registry, not the YAML
    dict (Pattern A — see critical-e2e-bugs.md)."""

    _PROVIDER = {
        "idp_id": "lemonldap",
        "idp_name": "LemonLDAP SSO",
        "issuer": "https://lemonldap.localhost/",
        "client_id": "synapse-demo",
        "client_secret": "demo-secret-change-in-prod",
        "scopes": ["openid"],
        "discover": False,
        "authorization_endpoint": "https://lemonldap.localhost/oauth2/authorize",
        "token_endpoint": "https://lemonldap.localhost/oauth2/token",
        "userinfo_endpoint": "https://lemonldap.localhost/oauth2/userinfo",
        "jwks_uri": "https://lemonldap.localhost/oauth2/jwks",
    }

    def test_iterates_registry_not_yaml_dict(self) -> None:
        """DB-driven mode: YAML dict empty, registry populated. Tenant
        providers must be built from the registry."""
        from synapse.handlers.oidc import OidcHandler

        acme = _make_tenant_with_oidc("acme.localhost", self._PROVIDER)
        hs = MagicMock()
        # YAML dict is empty (DB-driven mode).
        hs.config.multi_tenant = MagicMock(enabled=True, tenants={})
        # Registry populated.
        registry = MagicMock()
        registry.get_all_tenants.return_value = [acme]
        hs.get_tenant_registry.return_value = registry
        # Global providers must exist for OidcHandler construction, stub them.
        hs.config.oidc.oidc_providers = [MagicMock(idp_id="global")]
        hs.get_sso_handler.return_value = MagicMock()
        hs.get_macaroon_generator.return_value = MagicMock()

        # We don't exercise OidcProvider's full __init__ here — patch it.
        import synapse.handlers.oidc as oidc_mod
        from unittest.mock import patch

        with patch.object(oidc_mod, "OidcProvider") as OP, \
             patch.object(oidc_mod, "_parse_oidc_provider_configs",
                          return_value=[MagicMock(idp_id="lemonldap")]):
            handler = OidcHandler(hs)
            self.assertIn("acme.localhost", handler._tenant_providers)
            self.assertIn("lemonldap", handler._tenant_providers["acme.localhost"])
            OP.assert_called()  # at least once for the tenant provider
```

- [ ] **Step 2: Run probe — expect failure (reads YAML dict, sees empty)**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 trial tests.tenant.test_oidc_per_tenant.TestBuildTenantProvidersReadsRegistry 2>&1 | tail -15
```

Expected: FAIL. `_tenant_providers` is empty because `mt_config.tenants.values()` is empty.

- [ ] **Step 3: Patch `_build_tenant_providers` to read the registry**

In `synapse/handlers/oidc.py`, replace the body of `_build_tenant_providers` (lines 143-165) with:

```python
    def _build_tenant_providers(self, hs: "HomeServer") -> None:
        """Build OidcProvider sets for tenants with OIDC overrides.

        Consults the live ``hs.get_tenant_registry()`` rather than the
        YAML dict so DB-sourced tenants (source=database) are seen, and
        so control-plane reloads can cascade through
        :meth:`reload` below.
        """
        from synapse.config.oidc import _parse_oidc_provider_configs

        mt_config = getattr(hs.config, "multi_tenant", None)
        if not mt_config or not mt_config.enabled:
            return

        try:
            registry = hs.get_tenant_registry()
        except Exception:
            logger.debug("No tenant registry available; skipping tenant OIDC build")
            return

        for tenant in registry.get_all_tenants():
            if tenant.oidc is None or not tenant.oidc.providers:
                continue
            synthetic = {"oidc_providers": list(tenant.oidc.providers)}
            try:
                parsed = tuple(_parse_oidc_provider_configs(synthetic))
            except Exception:
                logger.exception(
                    "Failed to parse OIDC providers for tenant %s; skipping",
                    tenant.server_name,
                )
                continue
            self._tenant_providers[tenant.server_name] = {
                p.idp_id: OidcProvider(
                    hs,
                    self._macaroon_generator,
                    p,
                    tenant_server_name=tenant.server_name,
                    tenant_public_baseurl=tenant.effective_public_baseurl,
                )
                for p in parsed
            }
```

- [ ] **Step 4: Run probe — expect green**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 trial tests.tenant.test_oidc_per_tenant.TestBuildTenantProvidersReadsRegistry 2>&1 | tail -10
```

Expected: 1/1 pass.

- [ ] **Step 5: Regression check on existing OIDC tests**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 trial tests.handlers.test_oidc 2>&1 | tail -10
```

Expected: no new failures vs. baseline.

- [ ] **Step 6: Commit**

```bash
git add synapse/handlers/oidc.py tests/tenant/test_oidc_per_tenant.py
git commit -m "$(cat <<'EOF'
feat(oidc-b): build tenant OIDC providers from the live registry

Pattern A fix: _build_tenant_providers iterated hs.config.multi_tenant
.tenants.values() (the YAML dict, empty in DB-driven mode), so no
OidcProvider was ever built for DB-sourced tenants. Switch to
hs.get_tenant_registry().get_all_tenants() so both yaml and database
sources work, and so the upcoming OidcHandler.reload() cascade sees
the right tenant set.

Also: tolerate a missing registry (very-early startup) and isolate
per-tenant parse failures so one broken provider dict can't take the
whole handler down.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3 — oidc-c: Namespaced SSO registration + `OidcProvider` auto-register fix

**Sub-phase goal:** Two tenants sharing `idp_id="lemonldap"` no longer collide at startup. `SsoHandler.get_identity_providers()` filters by the current tenant ContextVar.

**Files:**
- Modify: `synapse/handlers/sso.py:229-243` + `get_identity_providers_for_user`
- Modify: `synapse/handlers/oidc.py:522` (skip auto-register when tenant-scoped) + `_build_tenant_providers` (explicit register)
- Test: `tests/tenant/test_oidc_per_tenant.py`

- [ ] **Step 1: Red probe — no-collision for two tenants with the same `idp_id`**

Append to `tests/tenant/test_oidc_per_tenant.py`:

```python
from synapse.tenant_context import set_current_tenant, reset_current_tenant


class TestSsoHandlerTenantAware(TestCase):
    """SsoHandler must accept per-tenant IdP registrations and filter
    get_identity_providers() by the current tenant ContextVar."""

    def _make_sso_handler(self):
        from synapse.handlers.sso import SsoHandler

        hs = MagicMock()
        hs.config.server.server_name = "main.localhost"
        hs.config.consent.user_consent_at_registration = False
        hs.get_clock.return_value = MagicMock()
        hs.get_auth_handler.return_value = MagicMock()
        hs.get_registration_handler.return_value = MagicMock()
        hs.get_datastores.return_value = MagicMock()
        hs.get_server_notices_manager.return_value = MagicMock()
        hs.get_replication_command_handler.return_value = MagicMock()
        return SsoHandler(hs)

    def _make_idp(self, idp_id: str):
        idp = MagicMock()
        idp.idp_id = idp_id
        return idp

    def test_two_tenants_same_idp_id_no_collision(self) -> None:
        sso = self._make_sso_handler()
        idp_a = self._make_idp("lemonldap")
        idp_b = self._make_idp("lemonldap")
        sso.register_tenant_identity_provider(idp_a, "acme.localhost")
        sso.register_tenant_identity_provider(idp_b, "corp.localhost")
        # Nothing raised = pass.

    def test_get_identity_providers_filters_by_tenant(self) -> None:
        sso = self._make_sso_handler()
        idp_a = self._make_idp("lemonldap")
        idp_b = self._make_idp("lemonldap")
        sso.register_tenant_identity_provider(idp_a, "acme.localhost")
        sso.register_tenant_identity_provider(idp_b, "corp.localhost")

        acme = _make_tenant_with_oidc("acme.localhost", {"idp_id": "lemonldap"})
        token = set_current_tenant(acme)
        try:
            providers = sso.get_identity_providers()
            # Key is bare idp_id after filtering; value is acme's idp.
            self.assertIn("lemonldap", providers)
            self.assertIs(providers["lemonldap"], idp_a)
        finally:
            reset_current_tenant(token)

    def test_global_providers_visible_when_no_tenant_context(self) -> None:
        sso = self._make_sso_handler()
        idp_global = self._make_idp("saml-global")
        sso.register_identity_provider(idp_global)
        providers = sso.get_identity_providers()
        self.assertIn("saml-global", providers)

    def test_deregister_tenant_identity_provider(self) -> None:
        sso = self._make_sso_handler()
        idp = self._make_idp("lemonldap")
        sso.register_tenant_identity_provider(idp, "acme.localhost")
        sso.deregister_tenant_identity_provider("acme.localhost", "lemonldap")
        acme = _make_tenant_with_oidc("acme.localhost", {"idp_id": "lemonldap"})
        token = set_current_tenant(acme)
        try:
            self.assertNotIn("lemonldap", sso.get_identity_providers())
        finally:
            reset_current_tenant(token)


class TestOidcProviderTenantScopedSkipsAutoRegister(TestCase):
    """OidcProvider.__init__ must NOT call register_identity_provider when
    tenant_server_name is set — the handler registers explicitly via
    register_tenant_identity_provider."""

    def test_tenant_scoped_oidc_provider_does_not_auto_register(self) -> None:
        from synapse.handlers.oidc import OidcProvider

        hs = MagicMock()
        hs.config.server.server_name = "main.localhost"
        hs.get_proxied_http_client.return_value = MagicMock()
        sso = MagicMock()
        hs.get_sso_handler.return_value = sso
        hs.get_device_handler.return_value = MagicMock()

        provider_cfg = MagicMock()
        provider_cfg.idp_id = "lemonldap"
        provider_cfg.idp_name = "LemonLDAP"
        provider_cfg.idp_icon = None
        provider_cfg.idp_brand = None
        provider_cfg.additional_authorization_parameters = ()
        provider_cfg.passthrough_authorization_parameters = ()

        # We only care about the register-call side-effect here. Patch the
        # heavy JWT / metadata init in OidcProvider.
        import synapse.handlers.oidc as oidc_mod
        from unittest.mock import patch

        with patch.object(oidc_mod.OidcProvider, "_init_with_provider",
                          return_value=None):
            OidcProvider(
                hs,
                MagicMock(),  # macaroon_generator
                provider_cfg,
                tenant_server_name="acme.localhost",
                tenant_public_baseurl="https://acme.localhost/",
            )
        sso.register_identity_provider.assert_not_called()
```

- [ ] **Step 2: Run probes — expect failure (methods don't exist; auto-register still fires)**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 trial tests.tenant.test_oidc_per_tenant.TestSsoHandlerTenantAware tests.tenant.test_oidc_per_tenant.TestOidcProviderTenantScopedSkipsAutoRegister 2>&1 | tail -20
```

Expected: multiple failures — `register_tenant_identity_provider` doesn't exist, `deregister_tenant_identity_provider` doesn't exist, auto-register still fires on tenant-scoped OidcProvider.

- [ ] **Step 3: Add tenant-aware methods to `SsoHandler`**

In `synapse/handlers/sso.py`, replace the registration methods (lines 229-243) with:

```python
        # map from registration key → SsoIdentityProvider.
        # Global IdPs are keyed by bare idp_id ("lemonldap").
        # Tenant-scoped IdPs are keyed "<tenant_server_name>::<idp_id>" so
        # multiple tenants can share an idp_id without colliding.
        self._identity_providers: dict[str, SsoIdentityProvider] = {}

        self._consent_at_registration = hs.config.consent.user_consent_at_registration

    # -- registration -------------------------------------------------

    def register_identity_provider(self, p: SsoIdentityProvider) -> None:
        """Register a GLOBAL identity provider (no tenant scoping)."""
        p_id = p.idp_id
        assert p_id not in self._identity_providers, (
            f"duplicate global identity provider registration for {p_id}"
        )
        self._identity_providers[p_id] = p
        init_counters_for_auth_provider(
            auth_provider_id=p_id, server_name=self.server_name
        )

    def register_tenant_identity_provider(
        self, p: SsoIdentityProvider, tenant_server_name: str
    ) -> None:
        """Register a TENANT-scoped identity provider.

        The same ``idp_id`` may be registered for multiple tenants; the
        storage key is namespaced (``<tenant>::<idp_id>``) so there is no
        collision.
        """
        key = f"{tenant_server_name}::{p.idp_id}"
        assert key not in self._identity_providers, (
            f"duplicate tenant identity provider registration for {key}"
        )
        self._identity_providers[key] = p
        init_counters_for_auth_provider(
            auth_provider_id=p.idp_id, server_name=tenant_server_name
        )

    def deregister_tenant_identity_provider(
        self, tenant_server_name: str, idp_id: str
    ) -> None:
        """Remove a tenant-scoped identity provider. Idempotent."""
        key = f"{tenant_server_name}::{idp_id}"
        self._identity_providers.pop(key, None)

    # -- lookup -------------------------------------------------------

    def get_identity_providers(self) -> Mapping[str, SsoIdentityProvider]:
        """Get the identity providers visible to the current request.

        Tenant context (via ContextVar) narrows the view:

          * If a tenant is current AND has tenant-scoped IdPs → return
            those (with the namespace stripped from the keys).
          * If a tenant is current but has no tenant-scoped IdPs → return
            the global IdPs (so yaml-only deployments still work).
          * If no tenant context → return the global IdPs.
        """
        from synapse.tenant_context import get_current_tenant

        tenant = get_current_tenant()
        if tenant is not None:
            prefix = f"{tenant.server_name}::"
            tenant_idps = {
                key[len(prefix):]: p
                for key, p in self._identity_providers.items()
                if key.startswith(prefix)
            }
            if tenant_idps:
                return tenant_idps
        # Global view: entries with no namespace separator.
        return {
            key: p
            for key, p in self._identity_providers.items()
            if "::" not in key
        }
```

- [ ] **Step 4: Patch `get_identity_providers_for_user` for namespaced lookup**

Still in `synapse/handlers/sso.py`, update the lookup inside `get_identity_providers_for_user` (around line 263):

```python
        for idp_id, _ in external_ids:
            # External IDs store the bare idp_id. Look up in both global
            # and any tenant namespace for a match.
            idp = self._identity_providers.get(idp_id)
            if idp is None:
                # Tenant-scoped lookup: find first key ending with ::<idp_id>.
                for key, candidate in self._identity_providers.items():
                    if key.endswith(f"::{idp_id}"):
                        idp = candidate
                        break
            if not idp:
                logger.warning(
                    "User %r has an SSO mapping for IdP %r, but this is no longer "
                    "configured.",
                    user_id,
                    idp_id,
                )
                continue
            valid_idps[idp_id] = idp
```

- [ ] **Step 5: Make `OidcProvider.__init__` skip auto-register when tenant-scoped**

In `synapse/handlers/oidc.py`, replace line 522 (`self._sso_handler.register_identity_provider(self)`) with:

```python
        # Tenant-scoped providers are registered explicitly by the
        # OidcHandler via ``register_tenant_identity_provider`` so the
        # storage key can be namespaced per tenant. Only auto-register
        # global providers here.
        if tenant_server_name is None:
            self._sso_handler.register_identity_provider(self)
```

- [ ] **Step 6: Register tenant providers explicitly in `_build_tenant_providers`**

In `synapse/handlers/oidc.py`, extend the `_build_tenant_providers` loop you wrote in Task 2. After building the dict for a tenant, register each provider:

```python
            built: dict[str, OidcProvider] = {
                p.idp_id: OidcProvider(
                    hs,
                    self._macaroon_generator,
                    p,
                    tenant_server_name=tenant.server_name,
                    tenant_public_baseurl=tenant.effective_public_baseurl,
                )
                for p in parsed
            }
            for provider in built.values():
                self._sso_handler.register_tenant_identity_provider(
                    provider, tenant.server_name
                )
            self._tenant_providers[tenant.server_name] = built
```

- [ ] **Step 7: Run probes — expect green**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 trial \
  tests.tenant.test_oidc_per_tenant.TestSsoHandlerTenantAware \
  tests.tenant.test_oidc_per_tenant.TestOidcProviderTenantScopedSkipsAutoRegister \
  tests.tenant.test_oidc_per_tenant.TestBuildTenantProvidersReadsRegistry \
  2>&1 | tail -10
```

Expected: all pass (existing Task-2 probe unaffected).

- [ ] **Step 8: Regression sweep**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 trial tests.handlers.test_oidc tests.handlers.test_sso 2>&1 | tail -10
```

Expected: no new failures vs. baseline. If `test_sso` didn't exist pre-phase, skip it.

- [ ] **Step 9: Commit**

```bash
git add synapse/handlers/sso.py synapse/handlers/oidc.py tests/tenant/test_oidc_per_tenant.py
git commit -m "$(cat <<'EOF'
feat(oidc-c): namespaced tenant IdP registration in SsoHandler

Multiple tenants sharing idp_id (the demo intentionally does: one shared
OIDC client via the proxy) collided on SsoHandler.register_identity
_provider's non-duplicate assert, crashing on the second provision.

Store tenant-scoped IdPs under "<tenant>::<idp_id>" via new
register_tenant_identity_provider / deregister_tenant_identity_provider
methods, and make get_identity_providers() filter by the current tenant
ContextVar — falling back to the global set when the tenant has no
overrides so yaml deployments are unchanged.

OidcProvider.__init__ skips the self-register call when tenant-scoped;
OidcHandler._build_tenant_providers registers each provider explicitly
with the namespaced API.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4 — oidc-d: `LoginRestServlet` per-request SSO gate

**Sub-phase goal:** `GET /_matrix/client/v3/login` advertises `m.login.sso` whenever the *current* tenant has OIDC providers, regardless of the global `oidc_enabled` flag.

**Files:**
- Modify: `synapse/rest/client/login.py:98, 144`
- Test: `tests/tenant/test_oidc_per_tenant.py`

- [ ] **Step 1: Red probe — tenant with OIDC but no global OIDC → advertise SSO**

Append to `tests/tenant/test_oidc_per_tenant.py`:

```python
class TestLoginAdvertisesTenantSso(TestCase):
    """LoginRestServlet.on_GET must advertise m.login.sso for a tenant
    whose OIDC config has providers, even when the global OIDC is off."""

    def _make_login_servlet(self, *, global_oidc_enabled: bool,
                            tenant_providers: dict):
        from synapse.rest.client.login import LoginRestServlet

        hs = MagicMock()
        hs.config.jwt.jwt_enabled = False
        hs.config.cas.cas_enabled = False
        hs.config.saml2.saml2_enabled = False
        hs.config.oidc.oidc_enabled = global_oidc_enabled
        hs.config.registration.refreshable_access_token_lifetime = None
        hs.config.experimental.msc3866.enabled = False
        hs.config.experimental.msc3866.require_approval_for_new_accounts = False
        hs.config.auth.login_via_existing_enabled = False
        hs.get_datastores.return_value.main = MagicMock()
        hs.get_auth.return_value = MagicMock()
        hs.get_clock.return_value = MagicMock()
        hs.get_auth_handler.return_value = MagicMock()
        hs.get_registration_handler.return_value = MagicMock()

        sso = MagicMock()
        sso.get_identity_providers.return_value = tenant_providers
        hs.get_sso_handler.return_value = sso

        oidc_handler = MagicMock()
        oidc_handler._get_providers.return_value = tenant_providers
        hs.get_oidc_handler.return_value = oidc_handler

        hs.get_module_api_callbacks.return_value = MagicMock()
        hs.get_account_validity_handler.return_value = MagicMock()
        hs.get_tenant_ratelimiter_registry.return_value = MagicMock()

        return LoginRestServlet(hs)

    def test_tenant_with_oidc_gets_sso_flow(self) -> None:
        idp = MagicMock()
        idp.idp_id = "lemonldap"
        idp.idp_name = "LemonLDAP SSO"
        idp.idp_icon = None
        idp.idp_brand = None
        servlet = self._make_login_servlet(
            global_oidc_enabled=False,
            tenant_providers={"lemonldap": idp},
        )
        request = MagicMock()
        _, body = servlet.on_GET(request)
        flows = body["flows"]
        sso_flows = [f for f in flows if f.get("type") == "m.login.sso"]
        self.assertEqual(len(sso_flows), 1)
        ids = [p["id"] for p in sso_flows[0]["identity_providers"]]
        self.assertIn("lemonldap", ids)

    def test_no_sso_when_neither_global_nor_tenant_configured(self) -> None:
        servlet = self._make_login_servlet(
            global_oidc_enabled=False,
            tenant_providers={},
        )
        request = MagicMock()
        _, body = servlet.on_GET(request)
        flows = body["flows"]
        sso_flows = [f for f in flows if f.get("type") == "m.login.sso"]
        self.assertEqual(sso_flows, [])
```

- [ ] **Step 2: Run probes — expect failure**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 trial tests.tenant.test_oidc_per_tenant.TestLoginAdvertisesTenantSso 2>&1 | tail -15
```

Expected: `test_tenant_with_oidc_gets_sso_flow` FAILS — the cached `self.oidc_enabled = False` gates out the SSO branch.

- [ ] **Step 3: Patch `LoginRestServlet` to compute the gate per request**

In `synapse/rest/client/login.py`:

(a) At line 98, replace:

```python
        self.oidc_enabled = hs.config.oidc.oidc_enabled
```

with:

```python
        self._global_oidc_enabled = hs.config.oidc.oidc_enabled
        # OidcHandler may be absent when no OIDC (global or tenant) is
        # ever configured; tolerate None on access.
        self._hs = hs
```

(b) Add a helper method above `on_GET`:

```python
    def _tenant_has_oidc(self) -> bool:
        """True if the current request's tenant (or the global config) has
        at least one OIDC provider.

        The global flag covers yaml-configured single-tenant deployments;
        the tenant-context branch covers DB-driven mode where global
        OIDC may be off but a specific tenant has providers.
        """
        if self._global_oidc_enabled:
            return True
        try:
            oidc = self._hs.get_oidc_handler()
        except Exception:
            return False
        if oidc is None:
            return False
        return bool(oidc._get_providers())
```

(c) At line 144, replace:

```python
        if self.cas_enabled or self.saml2_enabled or self.oidc_enabled:
```

with:

```python
        if self.cas_enabled or self.saml2_enabled or self._tenant_has_oidc():
```

- [ ] **Step 4: Run probes — expect green**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 trial tests.tenant.test_oidc_per_tenant.TestLoginAdvertisesTenantSso 2>&1 | tail -10
```

Expected: 2/2 pass.

- [ ] **Step 5: Regression — existing login tests**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 trial tests.rest.client.test_login 2>&1 | tail -10
```

Expected: no new failures. If any test mocks `self.oidc_enabled` directly, update it to mock `_global_oidc_enabled` instead.

- [ ] **Step 6: Commit**

```bash
git add synapse/rest/client/login.py tests/tenant/test_oidc_per_tenant.py
git commit -m "$(cat <<'EOF'
feat(oidc-d): per-request SSO gate in LoginRestServlet

The cached self.oidc_enabled flag was set at startup from
hs.config.oidc.oidc_enabled, which in DB-driven mode stays False
forever — the SSO branch of on_GET never fired even when the current
tenant had OIDC providers registered.

Replace the cached flag with _tenant_has_oidc(), which returns True if
the global is on OR the OidcHandler's tenant-context-aware _get_providers
returns anything. Single-tenant YAML deployments keep firing via the
global path.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5 — oidc-e: `OidcHandler.reload(registry)` + admin cascade wiring

**Sub-phase goal:** When the control plane PATCHes a tenant's `oidc_config` and calls `POST /_synapse/admin/v1/tenants/reload`, the OIDC handler rebuilds its `_tenant_providers` diff-style and updates `SsoHandler._identity_providers` — no Synapse restart.

**Files:**
- Modify: `synapse/handlers/oidc.py` (add `reload` method)
- Modify: `synapse/rest/admin/tenants.py:560-566` (cascade wiring)
- Test: `tests/tenant/test_oidc_per_tenant.py`

- [ ] **Step 1: Red probe — reload adds a newly-registered tenant, removes a deleted one**

Append to `tests/tenant/test_oidc_per_tenant.py`:

```python
class TestOidcHandlerReload(TestCase):
    """OidcHandler.reload diffs _tenant_providers against the live registry
    and updates SsoHandler registrations accordingly."""

    _PROVIDER = {
        "idp_id": "lemonldap",
        "idp_name": "LemonLDAP SSO",
        "issuer": "https://lemonldap.localhost/",
        "client_id": "synapse-demo",
        "client_secret": "demo-secret-change-in-prod",
        "scopes": ["openid"],
        "discover": False,
        "authorization_endpoint": "https://lemonldap.localhost/oauth2/authorize",
        "token_endpoint": "https://lemonldap.localhost/oauth2/token",
        "userinfo_endpoint": "https://lemonldap.localhost/oauth2/userinfo",
        "jwks_uri": "https://lemonldap.localhost/oauth2/jwks",
    }

    def _make_handler(self, tenants_at_start: list[TenantConfig]):
        from synapse.handlers.oidc import OidcHandler
        import synapse.handlers.oidc as oidc_mod
        from unittest.mock import patch

        hs = MagicMock()
        hs.config.multi_tenant = MagicMock(enabled=True, tenants={})
        registry = MagicMock()
        registry.get_all_tenants.return_value = tenants_at_start
        hs.get_tenant_registry.return_value = registry
        hs.config.oidc.oidc_providers = [MagicMock(idp_id="global")]
        hs.get_sso_handler.return_value = MagicMock()
        hs.get_macaroon_generator.return_value = MagicMock()

        with patch.object(oidc_mod, "OidcProvider") as OP, \
             patch.object(oidc_mod, "_parse_oidc_provider_configs",
                          return_value=[MagicMock(idp_id="lemonldap")]):
            handler = OidcHandler(hs)
        return handler, hs

    def test_reload_adds_new_tenant(self) -> None:
        handler, hs = self._make_handler(tenants_at_start=[])
        acme = _make_tenant_with_oidc("acme.localhost", self._PROVIDER)
        registry = hs.get_tenant_registry()
        registry.get_all_tenants.return_value = [acme]

        import synapse.handlers.oidc as oidc_mod
        from unittest.mock import patch
        with patch.object(oidc_mod, "OidcProvider"), \
             patch.object(oidc_mod, "_parse_oidc_provider_configs",
                          return_value=[MagicMock(idp_id="lemonldap")]):
            handler.reload(registry)

        self.assertIn("acme.localhost", handler._tenant_providers)
        sso = hs.get_sso_handler()
        sso.register_tenant_identity_provider.assert_any_call(
            # any provider object + acme.localhost
            handler._tenant_providers["acme.localhost"]["lemonldap"],
            "acme.localhost",
        )

    def test_reload_removes_deleted_tenant(self) -> None:
        acme = _make_tenant_with_oidc("acme.localhost", self._PROVIDER)
        handler, hs = self._make_handler(tenants_at_start=[acme])
        self.assertIn("acme.localhost", handler._tenant_providers)
        registry = hs.get_tenant_registry()
        registry.get_all_tenants.return_value = []

        import synapse.handlers.oidc as oidc_mod
        from unittest.mock import patch
        with patch.object(oidc_mod, "OidcProvider"), \
             patch.object(oidc_mod, "_parse_oidc_provider_configs",
                          return_value=[MagicMock(idp_id="lemonldap")]):
            handler.reload(registry)

        self.assertNotIn("acme.localhost", handler._tenant_providers)
        sso = hs.get_sso_handler()
        sso.deregister_tenant_identity_provider.assert_any_call(
            "acme.localhost", "lemonldap"
        )
```

- [ ] **Step 2: Run probes — expect failure (`reload` doesn't exist)**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 trial tests.tenant.test_oidc_per_tenant.TestOidcHandlerReload 2>&1 | tail -15
```

Expected: AttributeError — `OidcHandler` has no `reload`.

- [ ] **Step 3: Implement `OidcHandler.reload(registry)`**

In `synapse/handlers/oidc.py`, append to the `OidcHandler` class (right after `_get_providers`):

```python
    def reload(self, registry: "TenantRegistry") -> None:
        """Rebuild per-tenant OIDC providers against the live registry.

        Called from :class:`ReloadTenantsRestServlet` when the control
        plane pushes a tenant-config change. Diffs the new tenant set
        against the cached ``_tenant_providers`` and updates the
        :class:`SsoHandler` registrations in place.

        Best-effort: individual per-tenant parse / register failures are
        logged but don't abort the whole reload.
        """
        from synapse.config.oidc import _parse_oidc_provider_configs

        old: dict[str, dict[str, OidcProvider]] = self._tenant_providers
        new: dict[str, dict[str, OidcProvider]] = {}

        for tenant in registry.get_all_tenants():
            if tenant.oidc is None or not tenant.oidc.providers:
                continue
            synthetic = {"oidc_providers": list(tenant.oidc.providers)}
            try:
                parsed = tuple(_parse_oidc_provider_configs(synthetic))
            except Exception:
                logger.exception(
                    "Reload: failed to parse OIDC for tenant %s; skipping",
                    tenant.server_name,
                )
                continue
            new[tenant.server_name] = {
                p.idp_id: OidcProvider(
                    self._hs,
                    self._macaroon_generator,
                    p,
                    tenant_server_name=tenant.server_name,
                    tenant_public_baseurl=tenant.effective_public_baseurl,
                )
                for p in parsed
            }

        # De-register removed tenant IdPs.
        for server_name, providers in old.items():
            new_for_tenant = new.get(server_name, {})
            for idp_id in providers:
                if idp_id not in new_for_tenant:
                    try:
                        self._sso_handler.deregister_tenant_identity_provider(
                            server_name, idp_id
                        )
                    except Exception:
                        logger.exception(
                            "Reload: deregister failed for %s::%s",
                            server_name, idp_id,
                        )

        # Register new / updated tenant IdPs.
        for server_name, providers in new.items():
            old_for_tenant = old.get(server_name, {})
            for idp_id, provider in providers.items():
                if idp_id not in old_for_tenant:
                    try:
                        self._sso_handler.register_tenant_identity_provider(
                            provider, server_name
                        )
                    except Exception:
                        logger.exception(
                            "Reload: register failed for %s::%s",
                            server_name, idp_id,
                        )

        self._tenant_providers = new
        logger.info(
            "OidcHandler.reload: %d tenants with providers (was %d)",
            len(new), len(old),
        )
```

Also cache the `hs` reference in `__init__` if not already. Find `self._hs = hs` in `__init__` — it should already be there from line 125. If not, add `self._hs = hs` at the top of `__init__`.

- [ ] **Step 4: Wire the cascade in `ReloadTenantsRestServlet`**

In `synapse/rest/admin/tenants.py`, after line 566 (the ratelimiter reload) add:

```python
            # OIDC handler reload: rebuild per-tenant provider set and
            # (de)register tenant IdPs in the SsoHandler. Only runs if an
            # OidcHandler was constructed (i.e. OIDC is configured
            # globally or for at least one tenant at startup).
            try:
                oidc_handler = self._hs.get_oidc_handler()
            except Exception:
                oidc_handler = None
            if oidc_handler is not None:
                oidc_handler.reload(registry)
```

- [ ] **Step 5: Run probes — expect green**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 trial tests.tenant.test_oidc_per_tenant.TestOidcHandlerReload 2>&1 | tail -10
```

Expected: 2/2 pass.

- [ ] **Step 6: Full tenant-probe regression**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 trial tests.tenant.test_oidc_per_tenant 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add synapse/handlers/oidc.py synapse/rest/admin/tenants.py tests/tenant/test_oidc_per_tenant.py
git commit -m "$(cat <<'EOF'
feat(oidc-e): OidcHandler.reload + admin cascade wiring

Control-plane PATCH to oidc_config followed by a tenants/reload call
now rebuilds OidcHandler._tenant_providers and (de)registers the
matching SsoHandler entries — no Synapse restart.

Diff-style: tenants absent from the new registry have their IdPs
deregistered; tenants newly present get register_tenant_identity_
provider called. Per-tenant failures are logged and skipped so one
broken config can't abort the whole reload.

ReloadTenantsRestServlet cascades into oidc_handler.reload(registry)
alongside keyring.reload, tolerating a missing OidcHandler (single-
tenant deployments without OIDC).

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 8: Run `/sync-roadmap` — record phase OIDC sub-phases a–e complete**

Invoke the `sync-roadmap` skill (via the `Skill` tool):

```
Skill(skill="sync-roadmap")
```

The skill updates `roadmap-progess.md` and the animation workflow.

---

## Task 6 — oidc-f: E2E verification + Playwright smoke

**Sub-phase goal:** The docker-demo end-to-end smoke asserts `/login` returns `m.login.sso` for a freshly-provisioned tenant, and Element Web shows the SSO button.

**Files:**
- Modify: `docker-demo/scripts/test_tenants.py`
- No new Synapse test file; relies on the E2E + Playwright driver.

- [ ] **Step 1: Rebuild the Synapse image with the new handlers and bring up the demo**

```bash
cd docker-demo
docker compose down -v
docker volume prune -f
bash scripts/setup_certs.sh
docker compose build synapse
docker compose up -d
# Wait for synapse to be healthy
until curl -sk -o /dev/null -w '%{http_code}\n' https://acme.localhost/_matrix/client/versions | grep -q '200'; do sleep 2; done
```

- [ ] **Step 2: Provision a tenant via the manager UI or control plane**

Either:
- Open `http://localhost:3000`, create `acme.localhost` via the dialog, wait for the drawer's "oidc_config" step to go green; OR
- `curl -X POST http://localhost:3001/api/v1/tenants -H 'Content-Type: application/json' -d '{"server_name": "acme.localhost", "admin_user": "admin", "admin_password": "admin"}'`

- [ ] **Step 3: Assert `/login` advertises the LemonLDAP SSO flow**

```bash
curl -sk https://acme.localhost/_matrix/client/v3/login | python3 -m json.tool
```

Expected: the `flows` array contains an entry with `"type": "m.login.sso"` and an `identity_providers` list that includes `{"id": "lemonldap", "name": "LemonLDAP SSO", ...}`.

If this fails: re-check that `POST /_synapse/admin/v1/tenants/reload` fires (it is the control-plane's final provisioning step — `provisioning.ts`), and that `docker logs synapse-demo-synapse` shows `OidcHandler.reload: N tenants with providers`.

- [ ] **Step 4: Extend `docker-demo/scripts/test_tenants.py` with a `/login` assertion**

After the existing provisioning / federation test steps, add:

```python
def assert_login_advertises_sso(tenant: str, expected_idp_id: str = "lemonldap") -> None:
    """Verify GET /_matrix/client/v3/login returns m.login.sso with the
    expected IdP for a freshly-provisioned tenant."""
    resp = requests.get(
        f"https://{tenant}/_matrix/client/v3/login",
        verify=False,
        timeout=10,
    )
    resp.raise_for_status()
    flows = resp.json().get("flows", [])
    sso = [f for f in flows if f.get("type") == "m.login.sso"]
    assert sso, f"[{tenant}] m.login.sso flow missing; got {flows!r}"
    idp_ids = [p.get("id") for p in sso[0].get("identity_providers", [])]
    assert expected_idp_id in idp_ids, (
        f"[{tenant}] expected IdP {expected_idp_id!r}, got {idp_ids!r}"
    )
    print(f"[{tenant}] /login advertises {expected_idp_id} SSO flow ✓")
```

Call it from the main flow after tenant creation:

```python
    assert_login_advertises_sso("acme.localhost")
    assert_login_advertises_sso("corp.localhost")  # if the test creates a second
```

- [ ] **Step 5: Run the E2E script**

```bash
cd docker-demo
python3 scripts/test_tenants.py
```

Expected: the script prints `✓` for each tenant's SSO advertisement.

- [ ] **Step 6: Playwright browser smoke**

Use the `playwright-cli` skill:

1. Navigate to `https://acme.localhost/` (the Element Web container).
2. Locate a visible button whose accessible name contains `LemonLDAP SSO` (or the `idp_name` configured). Use the Playwright locator `getByRole('button', { name: /LemonLDAP SSO/i })`.
3. Click the button.
4. Wait for navigation and assert the final URL origin is `https://lemonldap.localhost` (the portal).

Document the command the operator runs. If the dev-mode Playwright skill is interactive, the operator drives it manually — but capture the exact URL asserts in the skill's scratchpad.

- [ ] **Step 7: Commit the E2E script changes**

```bash
git add docker-demo/scripts/test_tenants.py
git commit -m "$(cat <<'EOF'
test(oidc-f): assert /login advertises m.login.sso after provisioning

Extend the docker-demo smoke so a successful provisioning run is also
a successful verification that the per-tenant SSO flow reached the
client-facing /_matrix/client/v3/login response — guards against
regressions in the oidc-a..e chain.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 8: `/sync-roadmap` — record phase OIDC complete**

```
Skill(skill="sync-roadmap")
```

---

## Definition of Done (mirrors spec)

1. Fresh `docker compose up -d` + manager-driven provisioning → drawer shows all steps green including `oidc_config`.
2. `curl -sk https://acme.localhost/_matrix/client/v3/login | jq` shows `m.login.sso` with the LemonLDAP IdP.
3. Provisioning a second tenant does NOT crash Synapse on the `register_identity_provider` collision path.
4. Element Web shows a "Sign in with LemonLDAP SSO" button; click redirects to the LemonLDAP portal.
5. `alice@acme.localhost` / `demo` logs in end-to-end.
6. Deleting a tenant via the control plane (tenants/reload cascade) removes that tenant's IdP entries from `SsoHandler._identity_providers`.
7. `tests.handlers.test_oidc` and `tests.rest.client.test_login` remain green.
8. `tests.tenant.test_oidc_per_tenant` is entirely green.
9. `roadmap-progess.md` updated via `/sync-roadmap`.

---

## Risks Checklist (re-verify before each commit)

- [ ] No new `hs.config.multi_tenant.tenants.values()` / `.get()` / `.keys()` reads introduced. All tenant iteration via `hs.get_tenant_registry()`.
- [ ] `OidcProvider.__init__` only auto-registers when `tenant_server_name is None`.
- [ ] `SsoHandler.get_identity_providers()` falls back to the global subset when the current tenant has no tenant-scoped entries (so single-tenant YAML deployments don't break).
- [ ] `OidcHandler.reload` is best-effort on both the parse and (de)register steps — a single broken provider doesn't take the whole handler down.
- [ ] `LoginRestServlet._tenant_has_oidc` tolerates a missing `get_oidc_handler()` accessor.
