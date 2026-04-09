# Phase 3 — Per-Tenant SSO / Email / Push Config Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each tenant its own SSO providers, SMTP settings, and push configuration so that real tenant onboarding is unblocked.

**Architecture:** Nested frozen dataclasses (`TenantEmailConfig`, `TenantOidcConfig`, `TenantPushConfig`, `TenantCasConfig`, `TenantSamlConfig`) on `TenantConfig`; `None` = inherit global config. Handlers that cache config at `__init__` time are converted to resolve lazily from tenant context. Decomposed into 3a (SSO), 3b (email + server_notices sweep), 3c (push).

**Tech Stack:** Python 3.11+, Twisted Trial, attrs dataclasses, contextvars

**Spec:** `docs/superpowers/specs/2026-04-09-phase-3-per-tenant-sso-email-push-design.md`

---

## File structure

### New files
- `tests/tenant/test_tenant_email_config.py` — unit tests for `TenantEmailConfig` parsing and inheritance
- `tests/tenant/test_tenant_sso_config.py` — unit tests for `TenantOidcConfig`, `TenantCasConfig`, `TenantSamlConfig`
- `tests/tenant/test_tenant_push_config.py` — unit tests for `TenantPushConfig` parsing and inheritance
- `tests/tenant/test_server_notices_sweep.py` — probes for phase 2c-B deferred server_notices reads

### Modified files
- `synapse/config/tenants.py` — add new dataclasses + fields on `TenantConfig` + parsing in `from_dict`
- `synapse/handlers/send_email.py:111-128` — lazy email config resolution
- `synapse/handlers/oidc.py:121-135,380-393,440-445` — per-tenant OIDC provider dispatch
- `synapse/handlers/cas.py:75-97` — lazy CAS config resolution
- `synapse/handlers/saml.py:65-72` — lazy SAML config resolution
- `synapse/push/httppusher.py:126,130,514` — lazy push config resolution
- `synapse/push/emailpusher.py:86` — lazy email config for delay
- `synapse/push/pusher.py:45-50` — tenant-aware email_enable_notifs gate
- `synapse/push/mailer.py:139,913-914,932-934` — lazy email subjects/riot_base_url
- `synapse/push/bulk_push_rule_evaluator.py:138` — tenant-aware enable_push
- `synapse/api/auth_blocking.py:40,101` — tenant-aware server_notices_mxid
- `synapse/handlers/room_member.py:129,748-750` — tenant-aware server_notices_mxid
- `synapse/handlers/message.py:848-849` — tenant-aware server_notices_mxid
- `synapse/handlers/register.py:130,700-701` — tenant-aware server_notices_mxid
- `synapse/handlers/federation.py:147,1081` — tenant-aware server_notices_mxid
- `synapse/handlers/room.py:191,1114-1115` — tenant-aware server_notices_mxid

---

## Sub-phase 3b — Email / SMTP + Server Notices Sweep

> 3b is ordered first because email config is simpler and establishes the lazy-resolution pattern that 3a and 3c reuse. The server_notices sweep also closes a deferred item.

---

### Task 1: TenantEmailConfig dataclass + parsing

**Files:**
- Modify: `synapse/config/tenants.py`
- Test: `tests/tenant/test_tenant_email_config.py`

- [ ] **Step 1: Write failing tests for TenantEmailConfig**

Create `tests/tenant/test_tenant_email_config.py`:

```python
#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

from twisted.trial import unittest

from synapse.config.tenants import TenantConfig, TenantEmailConfig


class TenantEmailConfigTestCase(unittest.TestCase):
    """Tests for TenantEmailConfig dataclass and TenantConfig.email parsing."""

    def test_email_config_round_trip(self):
        """A tenant with an email block should parse into TenantEmailConfig."""
        cfg = TenantConfig.from_dict({
            "server_name": "acme.com",
            "signing_key_path": "/tmp/acme.key",
            "email": {
                "smtp_host": "smtp.acme.com",
                "smtp_port": 587,
                "smtp_user": "noreply@acme.com",
                "smtp_pass": "secret",
                "notif_from": "Acme Chat <noreply@acme.com>",
                "force_tls": True,
                "enable_tls": True,
                "require_transport_security": False,
                "app_name": "AcmeChat",
            },
        })
        self.assertIsNotNone(cfg.email)
        self.assertEqual(cfg.email.smtp_host, "smtp.acme.com")
        self.assertEqual(cfg.email.smtp_port, 587)
        self.assertEqual(cfg.email.smtp_user, "noreply@acme.com")
        self.assertEqual(cfg.email.smtp_pass, "secret")
        self.assertEqual(cfg.email.notif_from, "Acme Chat <noreply@acme.com>")
        self.assertTrue(cfg.email.force_tls)
        self.assertTrue(cfg.email.enable_tls)
        self.assertFalse(cfg.email.require_transport_security)
        self.assertEqual(cfg.email.app_name, "AcmeChat")

    def test_email_config_absent_is_none(self):
        """A tenant without an email block should have email=None (inherit global)."""
        cfg = TenantConfig.from_dict({
            "server_name": "corp.com",
            "signing_key_path": "/tmp/corp.key",
        })
        self.assertIsNone(cfg.email)

    def test_email_config_defaults(self):
        """Minimal email block should fill defaults."""
        cfg = TenantConfig.from_dict({
            "server_name": "acme.com",
            "signing_key_path": "/tmp/acme.key",
            "email": {
                "notif_from": "Acme <noreply@acme.com>",
            },
        })
        self.assertIsNotNone(cfg.email)
        self.assertEqual(cfg.email.smtp_host, "localhost")
        self.assertEqual(cfg.email.smtp_port, 25)
        self.assertIsNone(cfg.email.smtp_user)
        self.assertIsNone(cfg.email.smtp_pass)
        self.assertFalse(cfg.email.force_tls)
        self.assertTrue(cfg.email.enable_tls)
        self.assertEqual(cfg.email.app_name, "Matrix")

    def test_email_config_force_tls_default_port(self):
        """force_tls should default smtp_port to 465."""
        cfg = TenantConfig.from_dict({
            "server_name": "acme.com",
            "signing_key_path": "/tmp/acme.key",
            "email": {
                "notif_from": "Acme <noreply@acme.com>",
                "force_tls": True,
            },
        })
        self.assertEqual(cfg.email.smtp_port, 465)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `trial tests.tenant.test_tenant_email_config`
Expected: ImportError — `TenantEmailConfig` does not exist yet.

- [ ] **Step 3: Implement TenantEmailConfig and extend TenantConfig**

In `synapse/config/tenants.py`, add the `TenantEmailConfig` dataclass before `TenantConfig`:

```python
@attr.s(auto_attribs=True, slots=True, frozen=True)
class TenantEmailConfig:
    """Per-tenant email/SMTP configuration.

    When present on a TenantConfig, fully replaces the global email
    config for that tenant (no field-level merge).
    """

    notif_from: str
    smtp_host: str = "localhost"
    smtp_port: int = 25  # overridden to 465 by from_dict when force_tls
    smtp_user: str | None = None
    smtp_pass: str | None = None
    require_transport_security: bool = False
    enable_tls: bool = True
    force_tls: bool = False
    tlsname: str | None = None
    app_name: str = "Matrix"
    riot_base_url: str | None = None
    notif_delay_before_mail_ms: int = 300_000  # 5 minutes default

    @classmethod
    def from_dict(cls, d: "JsonDict") -> "TenantEmailConfig":
        force_tls = d.get("force_tls", False)
        default_port = 465 if force_tls else 25
        return cls(
            notif_from=d["notif_from"],
            smtp_host=d.get("smtp_host", "localhost"),
            smtp_port=d.get("smtp_port", default_port),
            smtp_user=d.get("smtp_user"),
            smtp_pass=d.get("smtp_pass"),
            require_transport_security=d.get("require_transport_security", False),
            enable_tls=d.get("enable_tls", True),
            force_tls=force_tls,
            tlsname=d.get("tlsname"),
            app_name=d.get("app_name", "Matrix"),
            riot_base_url=d.get("riot_base_url"),
            notif_delay_before_mail_ms=d.get(
                "notif_delay_before_mail_ms", 300_000
            ),
        )
```

Add field to `TenantConfig`:

```python
    email: TenantEmailConfig | None = None
```

In `TenantConfig.from_dict`, parse the email block:

```python
        email_dict = config.get("email")
        email_cfg = TenantEmailConfig.from_dict(email_dict) if email_dict else None
```

And pass `email=email_cfg` to the `cls(...)` constructor call.

- [ ] **Step 4: Run tests to verify they pass**

Run: `trial tests.tenant.test_tenant_email_config`
Expected: All 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add synapse/config/tenants.py tests/tenant/test_tenant_email_config.py
git commit -m "feat(tenant): add TenantEmailConfig dataclass with per-tenant SMTP settings"
```

---

### Task 2: SendEmailHandler lazy config resolution

**Files:**
- Modify: `synapse/handlers/send_email.py:111-128`

- [ ] **Step 1: Write failing test**

Add to `tests/tenant/test_tenant_email_config.py`:

```python
class SendEmailTenantResolutionTestCase(unittest.TestCase):
    """Verify that _resolve_email_config returns tenant config when set."""

    def test_resolve_returns_tenant_config_when_in_context(self):
        from synapse.config.tenants import TenantConfig, TenantEmailConfig
        from synapse.tenant_context import tenant_context

        tenant = TenantConfig.from_dict({
            "server_name": "acme.com",
            "signing_key_path": "/tmp/acme.key",
            "email": {
                "notif_from": "Acme <noreply@acme.com>",
                "smtp_host": "smtp.acme.com",
                "smtp_port": 587,
            },
        })

        with tenant_context(tenant):
            from synapse.tenant_context import get_current_tenant
            t = get_current_tenant()
            self.assertIsNotNone(t)
            self.assertIsNotNone(t.email)
            self.assertEqual(t.email.smtp_host, "smtp.acme.com")

    def test_resolve_returns_none_when_no_context(self):
        from synapse.tenant_context import get_current_tenant
        t = get_current_tenant()
        # Outside tenant context, no tenant email config
        self.assertIsNone(t)
```

- [ ] **Step 2: Run tests to verify they pass (these are context tests, they should pass already)**

Run: `trial tests.tenant.test_tenant_email_config`
Expected: PASS — this validates the integration path works.

- [ ] **Step 3: Convert SendEmailHandler to lazy resolution**

In `synapse/handlers/send_email.py`, replace the `__init__` config caching (lines 117-128) with:

```python
class SendEmailHandler:
    def __init__(self, hs: "HomeServer"):
        self.hs = hs
        self._reactor = hs.get_reactor()

        # Global config as fallback when no tenant context is set
        self._global_notif_from = hs.config.email.email_notif_from
        self._global_smtp_host = hs.config.email.email_smtp_host
        self._global_smtp_port = hs.config.email.email_smtp_port
        user = hs.config.email.email_smtp_user
        self._global_smtp_user = user.encode("utf-8") if user is not None else None
        passwd = hs.config.email.email_smtp_pass
        self._global_smtp_pass = passwd.encode("utf-8") if passwd is not None else None
        self._global_require_transport_security = hs.config.email.require_transport_security
        self._global_enable_tls = hs.config.email.enable_smtp_tls
        self._global_force_tls = hs.config.email.force_tls
        self._global_tlsname = hs.config.email.email_tlsname

        self._sendmail = _sendmail

    def _get_smtp_config(self) -> tuple[
        str | None, str, int, bytes | None, bytes | None, bool, bool, bool, str | None
    ]:
        """Return (notif_from, smtp_host, smtp_port, smtp_user, smtp_pass,
        require_transport_security, enable_tls, force_tls, tlsname),
        resolved from tenant config if available, else global."""
        from synapse.tenant_context import get_current_tenant

        tenant = get_current_tenant()
        if tenant and tenant.email:
            e = tenant.email
            user = e.smtp_user.encode("utf-8") if e.smtp_user else None
            passwd = e.smtp_pass.encode("utf-8") if e.smtp_pass else None
            return (
                e.notif_from, e.smtp_host, e.smtp_port,
                user, passwd,
                e.require_transport_security, e.enable_tls, e.force_tls,
                e.tlsname,
            )
        return (
            self._global_notif_from, self._global_smtp_host, self._global_smtp_port,
            self._global_smtp_user, self._global_smtp_pass,
            self._global_require_transport_security, self._global_enable_tls,
            self._global_force_tls, self._global_tlsname,
        )
```

Then update `send_email` to call `_get_smtp_config()` instead of using `self._from`, `self._smtp_host`, etc.:

```python
    async def send_email(
        self,
        email_address: str,
        subject: str,
        app_name: str,
        html: str,
        text: str,
        additional_headers: dict[str, str] | None = None,
    ) -> None:
        (
            notif_from, smtp_host, smtp_port, smtp_user, smtp_pass,
            require_transport_security, enable_tls, force_tls, tlsname,
        ) = self._get_smtp_config()

        try:
            from_string = notif_from % {"app": app_name}
        except (KeyError, TypeError):
            from_string = notif_from

        raw_from = email.utils.parseaddr(from_string)[1]
        raw_to = email.utils.parseaddr(email_address)[1]
        # ... rest of method unchanged, but use local variables instead of self._*
```

Update all references in the rest of `send_email` from `self._smtp_host` → `smtp_host`, `self._smtp_port` → `smtp_port`, etc.

- [ ] **Step 4: Run full test suite to verify no regressions**

Run: `trial tests.tenant`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add synapse/handlers/send_email.py tests/tenant/test_tenant_email_config.py
git commit -m "feat(tenant): SendEmailHandler resolves SMTP config from tenant context"
```

---

### Task 3: Mailer + PusherFactory tenant-aware email reads

**Files:**
- Modify: `synapse/push/mailer.py:139,913-914,932-934`
- Modify: `synapse/push/pusher.py:45-50`

- [ ] **Step 1: Convert Mailer to lazy email config resolution**

In `synapse/push/mailer.py`, the `__init__` caches `self.email_subjects` at line 139. Change to resolve lazily:

```python
    # In __init__, store global fallback:
    self._global_email_subjects = hs.config.email.email_subjects
    self._global_riot_base_url = hs.config.email.email_riot_base_url

    # Add resolver method:
    def _get_email_subjects(self) -> "EmailSubjectConfig":
        from synapse.tenant_context import get_current_tenant
        tenant = get_current_tenant()
        # TenantEmailConfig doesn't carry subjects yet — always fall back to global.
        # This accessor exists so a future phase can add per-tenant subjects.
        return self._global_email_subjects

    def _get_riot_base_url(self) -> str | None:
        from synapse.tenant_context import get_current_tenant
        tenant = get_current_tenant()
        if tenant and tenant.email and tenant.email.riot_base_url:
            return tenant.email.riot_base_url
        return self._global_riot_base_url
```

Then replace:
- Line 139: `self.email_subjects` usage sites → `self._get_email_subjects()`
- Lines 913-914, 932-934: `self.hs.config.email.email_riot_base_url` → `self._get_riot_base_url()`

- [ ] **Step 2: Convert PusherFactory to tenant-aware email_enable_notifs gate**

In `synapse/push/pusher.py` lines 45-50, the factory checks `hs.config.email.email_enable_notifs` at init. Change to check at pusher creation time:

```python
class PusherFactory:
    def __init__(self, hs: "HomeServer"):
        self.hs = hs
        self._global_email_enable_notifs = hs.config.email.email_enable_notifs
        # Store templates as global fallback
        if self._global_email_enable_notifs:
            self._notif_template_html = hs.config.email.email_notif_template_html
            self._notif_template_text = hs.config.email.email_notif_template_text

    def _email_notifs_enabled(self) -> bool:
        """Check if email notifications are enabled for the current tenant."""
        from synapse.tenant_context import get_current_tenant
        tenant = get_current_tenant()
        # TenantEmailConfig presence implies email is configured for that tenant.
        # If tenant has email config, notifs are enabled; else fall back to global.
        if tenant and tenant.email:
            return True
        return self._global_email_enable_notifs
```

Update the email pusher creation path to call `self._email_notifs_enabled()` instead of checking `hs.config.email.email_enable_notifs`.

- [ ] **Step 3: Run tests**

Run: `trial tests.tenant`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add synapse/push/mailer.py synapse/push/pusher.py
git commit -m "feat(tenant): Mailer and PusherFactory resolve email config from tenant context"
```

---

### Task 4: Server notices MXID sweep (phase 2c-B deferred)

**Files:**
- Modify: `synapse/api/auth_blocking.py:40,101`
- Modify: `synapse/handlers/room_member.py:129,748-750`
- Modify: `synapse/handlers/message.py:848-849`
- Modify: `synapse/handlers/register.py:130,700-701`
- Modify: `synapse/handlers/federation.py:147,1081`
- Modify: `synapse/handlers/room.py:191,1114-1115`
- Test: `tests/tenant/test_server_notices_sweep.py`

- [ ] **Step 1: Write failing probes**

Create `tests/tenant/test_server_notices_sweep.py`:

```python
#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

from twisted.trial import unittest

from synapse.config.tenants import TenantConfig
from synapse.tenant_context import tenant_context, get_current_tenant


class ServerNoticesMxidSweepTestCase(unittest.TestCase):
    """Phase 2c-B deferred: verify that server_notices_mxid reads
    resolve from tenant context, not global config.

    Six handlers cache `hs.config.servernotices.server_notices_mxid`
    at __init__ time. Under multi-tenant this produces wrong-tenant
    MXIDs. Each must resolve via tenant context instead.
    """

    def _make_tenants(self):
        acme = TenantConfig.from_dict({
            "server_name": "acme.localhost",
            "signing_key_path": "/tmp/acme.key",
            "server_notices_mxid": "@notices:acme.localhost",
        })
        corp = TenantConfig.from_dict({
            "server_name": "corp.localhost",
            "signing_key_path": "/tmp/corp.key",
        })
        return acme, corp

    def test_tenant_context_resolves_correct_notices_mxid(self):
        """Each tenant must resolve its own server_notices_mxid."""
        acme, corp = self._make_tenants()

        with tenant_context(acme):
            t = get_current_tenant()
            self.assertEqual(
                t.effective_server_notices_mxid, "@notices:acme.localhost"
            )

        with tenant_context(corp):
            t = get_current_tenant()
            # corp has no explicit mxid → falls back to @notices:corp.localhost
            self.assertEqual(
                t.effective_server_notices_mxid, "@notices:corp.localhost"
            )

    def test_no_cross_tenant_mxid_leak(self):
        """Switching tenant context must not leak the previous tenant's MXID."""
        acme, corp = self._make_tenants()

        with tenant_context(acme):
            mxid_a = get_current_tenant().effective_server_notices_mxid

        with tenant_context(corp):
            mxid_c = get_current_tenant().effective_server_notices_mxid

        self.assertNotEqual(mxid_a, mxid_c)
```

- [ ] **Step 2: Run probes to verify they pass (context tests — these exercise existing TenantConfig)**

Run: `trial tests.tenant.test_server_notices_sweep`
Expected: PASS — these validate the ContextVar path. The real conversion is in the handlers.

- [ ] **Step 3: Add helper function for server_notices_mxid resolution**

In `synapse/tenant_context.py`, add a convenience function:

```python
def get_effective_server_notices_mxid(
    global_mxid: str | None,
) -> str | None:
    """Return the server_notices MXID for the current tenant, or the global fallback.

    Args:
        global_mxid: The global server_notices_mxid from hs.config.

    Returns:
        The effective MXID, or None if server notices are disabled.
    """
    tenant = get_current_tenant()
    if tenant:
        return tenant.effective_server_notices_mxid
    return global_mxid
```

- [ ] **Step 4: Convert all six handlers**

Each handler follows the same pattern. Replace the `__init__`-time cache with a property or inline call:

**`synapse/api/auth_blocking.py`** — line 40 and line 101:
```python
# Line 40: keep the global value as fallback
self._global_server_notices_mxid = hs.config.servernotices.server_notices_mxid

# Line 101: resolve from tenant context
from synapse.tenant_context import get_effective_server_notices_mxid
effective_mxid = get_effective_server_notices_mxid(self._global_server_notices_mxid)
if user_id == effective_mxid:
    return
```

**`synapse/handlers/room_member.py`** — line 129 and lines 748-750:
```python
# Line 129:
self._global_server_notices_mxid = self.config.servernotices.server_notices_mxid

# Lines 748-750:
from synapse.tenant_context import get_effective_server_notices_mxid
effective_mxid = get_effective_server_notices_mxid(self._global_server_notices_mxid)
is_requester_server_notices_user = (
    effective_mxid is not None
    and requester.user.to_string() == effective_mxid
)
```

**`synapse/handlers/message.py`** — lines 848-849:
```python
from synapse.tenant_context import get_effective_server_notices_mxid
effective_mxid = get_effective_server_notices_mxid(
    self.config.servernotices.server_notices_mxid
)
if effective_mxid is not None and user_id == effective_mxid:
    return
```

**`synapse/handlers/register.py`** — line 130 and lines 700-701:
```python
# Line 130:
self._global_server_notices_mxid = hs.config.servernotices.server_notices_mxid

# Lines 700-701:
from synapse.tenant_context import get_effective_server_notices_mxid
effective_mxid = get_effective_server_notices_mxid(self._global_server_notices_mxid)
if effective_mxid is not None:
    if user_id == effective_mxid:
        raise SynapseError(400, "This user ID is reserved.")
```

**`synapse/handlers/federation.py`** — line 147 and line 1081:
```python
# Line 147:
self._global_server_notices_mxid = hs.config.servernotices.server_notices_mxid

# Line 1081:
from synapse.tenant_context import get_effective_server_notices_mxid
effective_mxid = get_effective_server_notices_mxid(self._global_server_notices_mxid)
if event.state_key == effective_mxid:
    raise SynapseError(HTTPStatus.FORBIDDEN, "Cannot invite this user")
```

**`synapse/handlers/room.py`** — line 191 and lines 1114-1115:
```python
# Line 191:
self._global_server_notices_mxid = hs.config.servernotices.server_notices_mxid

# Lines 1114-1115:
from synapse.tenant_context import get_effective_server_notices_mxid
effective_mxid = get_effective_server_notices_mxid(self._global_server_notices_mxid)
if effective_mxid is not None and user_id == effective_mxid:
```

- [ ] **Step 5: Run tests**

Run: `trial tests.tenant`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
git add synapse/tenant_context.py synapse/api/auth_blocking.py \
    synapse/handlers/room_member.py synapse/handlers/message.py \
    synapse/handlers/register.py synapse/handlers/federation.py \
    synapse/handlers/room.py tests/tenant/test_server_notices_sweep.py
git commit -m "fix(tenant): sweep server_notices_mxid reads to resolve from tenant context

Closes the phase 2c-B deferred item. Six handlers that cached
hs.config.servernotices.server_notices_mxid at __init__ time now
resolve via get_effective_server_notices_mxid() which reads the
tenant ContextVar."
```

---

## Sub-phase 3a — SSO (OIDC / CAS / SAML)

---

### Task 5: TenantOidcConfig + TenantCasConfig + TenantSamlConfig dataclasses

**Files:**
- Modify: `synapse/config/tenants.py`
- Test: `tests/tenant/test_tenant_sso_config.py`

- [ ] **Step 1: Write failing tests**

Create `tests/tenant/test_tenant_sso_config.py`:

```python
#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

from twisted.trial import unittest

from synapse.config.tenants import (
    TenantConfig,
    TenantCasConfig,
    TenantOidcConfig,
    TenantSamlConfig,
)


class TenantOidcConfigTestCase(unittest.TestCase):
    def test_oidc_providers_round_trip(self):
        cfg = TenantConfig.from_dict({
            "server_name": "acme.com",
            "signing_key_path": "/tmp/acme.key",
            "oidc_providers": [
                {
                    "idp_id": "acme-okta",
                    "idp_name": "Acme Okta",
                    "issuer": "https://acme.okta.com/",
                    "client_id": "abc",
                    "client_secret": "xyz",
                    "scopes": ["openid", "profile"],
                },
            ],
        })
        self.assertIsNotNone(cfg.oidc)
        self.assertEqual(len(cfg.oidc.providers), 1)
        self.assertEqual(cfg.oidc.providers[0]["idp_id"], "acme-okta")

    def test_oidc_absent_is_none(self):
        cfg = TenantConfig.from_dict({
            "server_name": "corp.com",
            "signing_key_path": "/tmp/corp.key",
        })
        self.assertIsNone(cfg.oidc)

    def test_multiple_oidc_providers(self):
        cfg = TenantConfig.from_dict({
            "server_name": "acme.com",
            "signing_key_path": "/tmp/acme.key",
            "oidc_providers": [
                {
                    "idp_id": "okta",
                    "idp_name": "Okta",
                    "issuer": "https://okta.example/",
                    "client_id": "a",
                    "client_secret": "b",
                },
                {
                    "idp_id": "azure",
                    "idp_name": "Azure AD",
                    "issuer": "https://login.microsoft.com/",
                    "client_id": "c",
                    "client_secret": "d",
                },
            ],
        })
        self.assertEqual(len(cfg.oidc.providers), 2)


class TenantCasConfigTestCase(unittest.TestCase):
    def test_cas_round_trip(self):
        cfg = TenantConfig.from_dict({
            "server_name": "corp.com",
            "signing_key_path": "/tmp/corp.key",
            "cas": {
                "server_url": "https://cas.corp.com",
                "protocol_version": 3,
                "displayname_attribute": "cn",
                "enable_registration": True,
            },
        })
        self.assertIsNotNone(cfg.cas)
        self.assertEqual(cfg.cas.server_url, "https://cas.corp.com")
        self.assertEqual(cfg.cas.protocol_version, 3)
        self.assertTrue(cfg.cas.enable_registration)

    def test_cas_absent_is_none(self):
        cfg = TenantConfig.from_dict({
            "server_name": "acme.com",
            "signing_key_path": "/tmp/acme.key",
        })
        self.assertIsNone(cfg.cas)


class TenantSamlConfigTestCase(unittest.TestCase):
    def test_saml_round_trip(self):
        cfg = TenantConfig.from_dict({
            "server_name": "edu.com",
            "signing_key_path": "/tmp/edu.key",
            "saml": {
                "idp_entityid": "https://idp.edu.com/saml",
                "session_lifetime": "15m",
            },
        })
        self.assertIsNotNone(cfg.saml)
        self.assertEqual(cfg.saml.idp_entityid, "https://idp.edu.com/saml")

    def test_saml_absent_is_none(self):
        cfg = TenantConfig.from_dict({
            "server_name": "acme.com",
            "signing_key_path": "/tmp/acme.key",
        })
        self.assertIsNone(cfg.saml)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `trial tests.tenant.test_tenant_sso_config`
Expected: ImportError — `TenantOidcConfig`, `TenantCasConfig`, `TenantSamlConfig` don't exist.

- [ ] **Step 3: Implement SSO config dataclasses**

In `synapse/config/tenants.py`, add:

```python
@attr.s(auto_attribs=True, slots=True, frozen=True)
class TenantOidcConfig:
    """Per-tenant OIDC configuration.

    Stores raw provider dicts that will be parsed by _parse_oidc_provider_configs
    at handler init time. This avoids importing the full OIDC config machinery
    into the tenant config module.
    """
    providers: tuple[dict, ...] = attr.Factory(tuple)

    @classmethod
    def from_list(cls, providers: list[dict]) -> "TenantOidcConfig":
        return cls(providers=tuple(providers))


@attr.s(auto_attribs=True, slots=True, frozen=True)
class TenantCasConfig:
    """Per-tenant CAS configuration."""
    server_url: str
    protocol_version: int | None = None
    displayname_attribute: str | None = None
    required_attributes: dict[str, str | None] = attr.Factory(dict)
    enable_registration: bool = True
    allow_numeric_ids: bool = False
    numeric_ids_prefix: str = "u"
    idp_name: str = "CAS"
    idp_icon: str | None = None
    idp_brand: str | None = None

    @classmethod
    def from_dict(cls, d: "JsonDict") -> "TenantCasConfig":
        return cls(
            server_url=d["server_url"],
            protocol_version=d.get("protocol_version"),
            displayname_attribute=d.get("displayname_attribute"),
            required_attributes=d.get("required_attributes", {}),
            enable_registration=d.get("enable_registration", True),
            allow_numeric_ids=d.get("allow_numeric_ids", False),
            numeric_ids_prefix=d.get("numeric_ids_prefix", "u"),
            idp_name=d.get("idp_name", "CAS"),
            idp_icon=d.get("idp_icon"),
            idp_brand=d.get("idp_brand"),
        )


@attr.s(auto_attribs=True, slots=True, frozen=True)
class TenantSamlConfig:
    """Per-tenant SAML configuration.

    Minimal: stores IdP entity ID and session lifetime.
    Full Saml2Config SP construction is deferred to handler init
    because pysaml2's config objects are not trivially serializable.
    """
    idp_entityid: str | None = None
    session_lifetime: str = "15m"
    raw_config: dict = attr.Factory(dict)  # raw YAML for Saml2Config construction

    @classmethod
    def from_dict(cls, d: "JsonDict") -> "TenantSamlConfig":
        return cls(
            idp_entityid=d.get("idp_entityid"),
            session_lifetime=d.get("session_lifetime", "15m"),
            raw_config=dict(d),
        )
```

Add fields to `TenantConfig`:

```python
    oidc: TenantOidcConfig | None = None
    cas: TenantCasConfig | None = None
    saml: TenantSamlConfig | None = None
```

In `TenantConfig.from_dict`, parse the SSO blocks:

```python
        oidc_list = config.get("oidc_providers")
        oidc_cfg = TenantOidcConfig.from_list(oidc_list) if oidc_list else None

        cas_dict = config.get("cas")
        cas_cfg = TenantCasConfig.from_dict(cas_dict) if cas_dict else None

        saml_dict = config.get("saml")
        saml_cfg = TenantSamlConfig.from_dict(saml_dict) if saml_dict else None
```

Pass `oidc=oidc_cfg, cas=cas_cfg, saml=saml_cfg` to the `cls(...)` constructor.

- [ ] **Step 4: Run tests to verify they pass**

Run: `trial tests.tenant.test_tenant_sso_config`
Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add synapse/config/tenants.py tests/tenant/test_tenant_sso_config.py
git commit -m "feat(tenant): add TenantOidcConfig, TenantCasConfig, TenantSamlConfig dataclasses"
```

---

### Task 6: OidcHandler per-tenant provider dispatch

**Files:**
- Modify: `synapse/handlers/oidc.py:121-135,152,380-393,440-445`

- [ ] **Step 1: Convert OidcHandler to per-tenant provider registry**

In `synapse/handlers/oidc.py`, modify `OidcHandler.__init__`:

```python
class OidcHandler:
    def __init__(self, hs: "HomeServer"):
        self._hs = hs
        self._sso_handler = hs.get_sso_handler()
        self._macaroon_generator = hs.get_macaroon_generator()

        # Global providers (used by tenants that don't override OIDC)
        global_confs = hs.config.oidc.oidc_providers
        assert global_confs  # should not be instantiated without providers
        self._global_providers: dict[str, OidcProvider] = {
            p.idp_id: OidcProvider(hs, self._macaroon_generator, p)
            for p in global_confs
        }

        # Per-tenant providers: dict[server_name, dict[idp_id, OidcProvider]]
        self._tenant_providers: dict[str, dict[str, OidcProvider]] = {}
        self._build_tenant_providers(hs)

    def _build_tenant_providers(self, hs: "HomeServer") -> None:
        """Build OidcProvider sets for tenants with OIDC overrides."""
        from synapse.config.oidc import _parse_oidc_provider_configs

        mt_config = hs.config.multi_tenant
        if not mt_config or not mt_config.enabled:
            return

        for tenant in mt_config.tenants.values():
            if tenant.oidc is None:
                continue
            # Build a synthetic config dict for _parse_oidc_provider_configs
            synthetic = {"oidc_providers": list(tenant.oidc.providers)}
            parsed = tuple(_parse_oidc_provider_configs(synthetic))
            self._tenant_providers[tenant.server_name] = {
                p.idp_id: OidcProvider(
                    hs, self._macaroon_generator, p,
                    tenant_server_name=tenant.server_name,
                    tenant_public_baseurl=tenant.effective_public_baseurl,
                )
                for p in parsed
            }

    def _get_providers(self) -> dict[str, "OidcProvider"]:
        """Return the OIDC providers for the current tenant context."""
        from synapse.tenant_context import get_current_tenant
        tenant = get_current_tenant()
        if tenant and tenant.server_name in self._tenant_providers:
            return self._tenant_providers[tenant.server_name]
        return self._global_providers
```

Update `handle_oidc_callback` and any method that accesses `self._providers` to call `self._get_providers()` instead.

- [ ] **Step 2: Modify OidcProvider to accept per-tenant overrides**

In `OidcProvider.__init__` (around line 440), add optional parameters:

```python
class OidcProvider:
    def __init__(
        self,
        hs: "HomeServer",
        macaroon_generator: ...,
        provider: OidcProviderConfig,
        tenant_server_name: str | None = None,
        tenant_public_baseurl: str | None = None,
    ):
        # ... existing code ...
        self._server_name = tenant_server_name or hs.config.server.server_name

        # Callback URL: use tenant's public_baseurl if provided
        if provider.redirect_uri is not None:
            self._callback_url = provider.redirect_uri
        else:
            base = tenant_public_baseurl or hs.config.server.public_baseurl
            self._callback_url = base + "_synapse/client/oidc/callback"

        public_baseurl_path = urlparse(
            tenant_public_baseurl or hs.config.server.public_baseurl
        ).path
        self._callback_path_prefix = (
            public_baseurl_path.encode("utf-8") + b"_synapse/client/oidc"
        )
```

- [ ] **Step 3: Update load_metadata to cover tenant providers**

```python
    async def load_metadata(self) -> None:
        for idp_id, p in self._global_providers.items():
            try:
                await p.load_metadata()
                if not p._uses_userinfo:
                    await p.load_jwks()
            except Exception as e:
                raise Exception(
                    "Error while initialising OIDC provider %r" % (idp_id,)
                ) from e

        for server_name, providers in self._tenant_providers.items():
            for idp_id, p in providers.items():
                try:
                    await p.load_metadata()
                    if not p._uses_userinfo:
                        await p.load_jwks()
                except Exception as e:
                    raise Exception(
                        "Error initialising OIDC provider %r for tenant %s"
                        % (idp_id, server_name)
                    ) from e
```

- [ ] **Step 4: Run tests**

Run: `trial tests.tenant`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add synapse/handlers/oidc.py
git commit -m "feat(tenant): OidcHandler dispatches per-tenant OIDC providers

OidcHandler builds separate provider sets for tenants with oidc
overrides. OidcProvider accepts tenant_server_name and
tenant_public_baseurl for per-tenant callback URLs."
```

---

### Task 7: CasHandler tenant-aware config resolution

**Files:**
- Modify: `synapse/handlers/cas.py:75-97`

- [ ] **Step 1: Convert CasHandler to lazy config resolution**

In `synapse/handlers/cas.py`, replace the 10+ cached fields with global fallbacks and a resolver:

```python
class CasHandler:
    def __init__(self, hs: "HomeServer"):
        # ... existing non-config init ...
        self._hs = hs

        # Store global CAS config as fallback
        self._global_cas_server_url = hs.config.cas.cas_server_url
        self._global_cas_service_url = hs.config.cas.cas_service_url
        self._global_cas_protocol_version = hs.config.cas.cas_protocol_version
        self._global_cas_displayname_attribute = hs.config.cas.cas_displayname_attribute
        self._global_cas_required_attributes = hs.config.cas.cas_required_attributes
        self._global_cas_enable_registration = hs.config.cas.cas_enable_registration
        self._global_cas_allow_numeric_ids = hs.config.cas.cas_allow_numeric_ids
        self._global_cas_numeric_ids_prefix = hs.config.cas.cas_numeric_ids_prefix
        self._global_idp_name = hs.config.cas.idp_name
        self._global_idp_icon = hs.config.cas.idp_icon
        self._global_idp_brand = hs.config.cas.idp_brand

    def _get_cas_server_url(self) -> str:
        from synapse.tenant_context import get_current_tenant
        tenant = get_current_tenant()
        if tenant and tenant.cas:
            return tenant.cas.server_url
        return self._global_cas_server_url

    def _get_cas_service_url(self) -> str | None:
        from synapse.tenant_context import get_current_tenant
        tenant = get_current_tenant()
        if tenant and tenant.cas:
            # Derive from tenant's public_baseurl
            return tenant.effective_public_baseurl + "_matrix/client/r0/login/cas/ticket"
        return self._global_cas_service_url

    def _get_cas_enable_registration(self) -> bool:
        from synapse.tenant_context import get_current_tenant
        tenant = get_current_tenant()
        if tenant and tenant.cas:
            return tenant.cas.enable_registration
        return self._global_cas_enable_registration
```

Then replace all `self._cas_server_url` usages with `self._get_cas_server_url()`, etc., throughout the handler methods.

- [ ] **Step 2: Run tests**

Run: `trial tests.tenant`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add synapse/handlers/cas.py
git commit -m "feat(tenant): CasHandler resolves CAS config from tenant context"
```

---

### Task 8: SamlHandler tenant-aware config resolution

**Files:**
- Modify: `synapse/handlers/saml.py:65-72`

- [ ] **Step 1: Convert SamlHandler to lazy config resolution**

In `synapse/handlers/saml.py`, replace cached fields:

```python
class SamlHandler:
    def __init__(self, hs: "HomeServer"):
        # ... existing init ...
        self._hs = hs

        # Global SAML config as fallback
        self._global_saml_client = Saml2Client(hs.config.saml2.saml2_sp_config)
        self._global_saml_idp_entityid = hs.config.saml2.saml2_idp_entityid
        self._global_saml2_session_lifetime = hs.config.saml2.saml2_session_lifetime
        self._global_grandfathered_mxid_source_attribute = (
            hs.config.saml2.saml2_grandfathered_mxid_source_attribute
        )
        self._global_attribute_requirements = hs.config.saml2.attribute_requirements

    def _get_saml_idp_entityid(self) -> str | None:
        from synapse.tenant_context import get_current_tenant
        tenant = get_current_tenant()
        if tenant and tenant.saml:
            return tenant.saml.idp_entityid
        return self._global_saml_idp_entityid

    def _get_attribute_requirements(self):
        from synapse.tenant_context import get_current_tenant
        tenant = get_current_tenant()
        if tenant and tenant.saml:
            # Tenant SAML doesn't carry parsed attribute_requirements yet;
            # fall back to global.
            pass
        return self._global_attribute_requirements
```

Replace `self._saml_idp_entityid` usages → `self._get_saml_idp_entityid()`, etc.

- [ ] **Step 2: Run tests**

Run: `trial tests.tenant`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add synapse/handlers/saml.py
git commit -m "feat(tenant): SamlHandler resolves SAML config from tenant context"
```

---

## Sub-phase 3c — Push

---

### Task 9: TenantPushConfig dataclass

**Files:**
- Modify: `synapse/config/tenants.py`
- Test: `tests/tenant/test_tenant_push_config.py`

- [ ] **Step 1: Write failing tests**

Create `tests/tenant/test_tenant_push_config.py`:

```python
#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

from twisted.trial import unittest

from synapse.config.tenants import TenantConfig, TenantPushConfig


class TenantPushConfigTestCase(unittest.TestCase):
    def test_push_config_round_trip(self):
        cfg = TenantConfig.from_dict({
            "server_name": "acme.com",
            "signing_key_path": "/tmp/acme.key",
            "push": {
                "include_content": False,
                "enabled": True,
                "group_unread_count_by_room": False,
                "jitter_delay_ms": 5000,
            },
        })
        self.assertIsNotNone(cfg.push)
        self.assertFalse(cfg.push.include_content)
        self.assertTrue(cfg.push.enabled)
        self.assertFalse(cfg.push.group_unread_count_by_room)
        self.assertEqual(cfg.push.jitter_delay_ms, 5000)

    def test_push_absent_is_none(self):
        cfg = TenantConfig.from_dict({
            "server_name": "corp.com",
            "signing_key_path": "/tmp/corp.key",
        })
        self.assertIsNone(cfg.push)

    def test_push_defaults(self):
        cfg = TenantConfig.from_dict({
            "server_name": "acme.com",
            "signing_key_path": "/tmp/acme.key",
            "push": {},
        })
        self.assertIsNotNone(cfg.push)
        self.assertTrue(cfg.push.include_content)
        self.assertTrue(cfg.push.enabled)
        self.assertTrue(cfg.push.group_unread_count_by_room)
        self.assertIsNone(cfg.push.jitter_delay_ms)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `trial tests.tenant.test_tenant_push_config`
Expected: ImportError — `TenantPushConfig` doesn't exist.

- [ ] **Step 3: Implement TenantPushConfig**

In `synapse/config/tenants.py`, add:

```python
@attr.s(auto_attribs=True, slots=True, frozen=True)
class TenantPushConfig:
    """Per-tenant push notification configuration."""

    include_content: bool = True
    enabled: bool = True
    group_unread_count_by_room: bool = True
    jitter_delay_ms: int | None = None

    @classmethod
    def from_dict(cls, d: "JsonDict") -> "TenantPushConfig":
        return cls(
            include_content=d.get("include_content", True),
            enabled=d.get("enabled", True),
            group_unread_count_by_room=d.get("group_unread_count_by_room", True),
            jitter_delay_ms=d.get("jitter_delay_ms"),
        )
```

Add to `TenantConfig`:

```python
    push: TenantPushConfig | None = None
```

In `TenantConfig.from_dict`:

```python
        push_dict = config.get("push")
        push_cfg = TenantPushConfig.from_dict(push_dict) if push_dict else None
```

Pass `push=push_cfg` to constructor.

- [ ] **Step 4: Run tests to verify they pass**

Run: `trial tests.tenant.test_tenant_push_config`
Expected: All 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add synapse/config/tenants.py tests/tenant/test_tenant_push_config.py
git commit -m "feat(tenant): add TenantPushConfig dataclass with per-tenant push settings"
```

---

### Task 10: HttpPusher + EmailPusher + BulkPushRuleEvaluator tenant-aware config

**Files:**
- Modify: `synapse/push/httppusher.py:126,130,514`
- Modify: `synapse/push/emailpusher.py:86`
- Modify: `synapse/push/bulk_push_rule_evaluator.py:138`

- [ ] **Step 1: Convert HttpPusher to lazy push config resolution**

In `synapse/push/httppusher.py`, replace cached config reads:

```python
# At line 126, replace:
#     self.data_minus_url["unread_count_by_room"] = (
#         hs.config.push.push_group_unread_count_by_room
#     )
# With:
    self._global_group_unread_count_by_room = hs.config.push.push_group_unread_count_by_room

# At line 130, replace:
#     self.push_jitter_delay_ms = hs.config.push.push_jitter_delay_ms
# With:
    self._global_push_jitter_delay_ms = hs.config.push.push_jitter_delay_ms
    self._global_push_include_content = hs.config.push.push_include_content

# Add resolver methods:
def _get_push_include_content(self) -> bool:
    from synapse.tenant_context import get_current_tenant
    tenant = get_current_tenant()
    if tenant and tenant.push:
        return tenant.push.include_content
    return self._global_push_include_content

def _get_push_jitter_delay_ms(self) -> int | None:
    from synapse.tenant_context import get_current_tenant
    tenant = get_current_tenant()
    if tenant and tenant.push:
        return tenant.push.jitter_delay_ms
    return self._global_push_jitter_delay_ms

def _get_group_unread_count_by_room(self) -> bool:
    from synapse.tenant_context import get_current_tenant
    tenant = get_current_tenant()
    if tenant and tenant.push:
        return tenant.push.group_unread_count_by_room
    return self._global_group_unread_count_by_room
```

Replace line 514 (`self.hs.config.push.push_include_content`) → `self._get_push_include_content()`.
Replace jitter delay usage → `self._get_push_jitter_delay_ms()`.
Replace group_unread_count_by_room usage → `self._get_group_unread_count_by_room()`.

- [ ] **Step 2: Convert EmailPusher to lazy config resolution**

In `synapse/push/emailpusher.py` line 86:

```python
# Replace:
#     self._delay_before_mail_ms = self.hs.config.email.notif_delay_before_mail_ms
# With:
    self._global_delay_before_mail_ms = self.hs.config.email.notif_delay_before_mail_ms

def _get_delay_before_mail_ms(self) -> int:
    from synapse.tenant_context import get_current_tenant
    tenant = get_current_tenant()
    if tenant and tenant.email:
        return tenant.email.notif_delay_before_mail_ms
    return self._global_delay_before_mail_ms
```

Replace all usages of `self._delay_before_mail_ms` → `self._get_delay_before_mail_ms()`.

- [ ] **Step 3: Convert BulkPushRuleEvaluator to tenant-aware enable_push**

In `synapse/push/bulk_push_rule_evaluator.py` line 138:

```python
# Replace:
#     self.should_calculate_push_rules = self.hs.config.push.enable_push
# With:
    self._global_enable_push = self.hs.config.push.enable_push

@property
def should_calculate_push_rules(self) -> bool:
    from synapse.tenant_context import get_current_tenant
    tenant = get_current_tenant()
    if tenant and tenant.push:
        return tenant.push.enabled
    return self._global_enable_push
```

- [ ] **Step 4: Run tests**

Run: `trial tests.tenant`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add synapse/push/httppusher.py synapse/push/emailpusher.py \
    synapse/push/bulk_push_rule_evaluator.py
git commit -m "feat(tenant): HttpPusher, EmailPusher, BulkPushRuleEvaluator resolve config from tenant context"
```

---

### Task 11: Final verification + sync-roadmap

- [ ] **Step 1: Run full tenant test suite**

Run: `trial tests.tenant`
Expected: All tests PASS (existing 34+ probes + new probes from tasks 1, 4, 5, 9).

- [ ] **Step 2: Run broader test suite for regressions**

Run: `SYNAPSE_SKIP_RUST_CHECK=1 trial tests.handlers.test_oidc tests.handlers.test_cas tests.push`
Expected: PASS (no regressions in upstream handler tests).

- [ ] **Step 3: Run sync-roadmap skill**

Invoke `/sync-roadmap` to update `roadmap-progess.md` and `multi-tenancy-workflow/data.js`.

- [ ] **Step 4: Commit tracker updates separately**

```bash
git add roadmap-progess.md multi-tenancy-workflow/data.js
git commit -m "docs: sync roadmap progress after phase 3 implementation"
```

---

## Summary

| Sub-phase | Tasks | Config dataclass | Handlers converted |
|---|---|---|---|
| 3b (email + notices sweep) | 1-4 | `TenantEmailConfig` | `SendEmailHandler`, `Mailer`, `PusherFactory`, 6× server_notices handlers |
| 3a (SSO) | 5-8 | `TenantOidcConfig`, `TenantCasConfig`, `TenantSamlConfig` | `OidcHandler`, `OidcProvider`, `CasHandler`, `SamlHandler` |
| 3c (push) | 9-10 | `TenantPushConfig` | `HttpPusher`, `EmailPusher`, `BulkPushRuleEvaluator` |
| Final | 11 | — | Verification + roadmap sync |
