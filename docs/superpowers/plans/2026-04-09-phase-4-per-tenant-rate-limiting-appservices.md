# Phase 4 — Per-Tenant Rate Limiting + App Services Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each tenant its own rate-limit buckets with independently tunable settings, and scope app-service registrations per-tenant so bridges/bots are isolated.

**Architecture:** Frozen `TenantRatelimitConfig` dataclass on `TenantConfig` with per-field `RatelimitSettings | None` inheritance. A `TenantRatelimiterRegistry` singleton on `HomeServer` lazily creates per-tenant `Ratelimiter` instances. Handlers resolve limiters at request time via tenant context. App services get a `TenantAppServiceRegistry` that loads per-tenant AS lists at startup; store lookup methods dispatch on `get_current_tenant()`.

**Tech Stack:** Python 3.11+, Twisted Trial, attrs dataclasses, contextvars

**Spec:** `docs/superpowers/specs/2026-04-09-phase-4-per-tenant-rate-limiting-appservices-design.md`

---

## File structure

### New files
- `synapse/api/tenant_ratelimiting.py` — `TenantRatelimiterRegistry` class
- `synapse/appservice/tenant_registry.py` — `TenantAppServiceRegistry` class
- `tests/tenant/test_tenant_ratelimit_config.py` — unit tests for `TenantRatelimitConfig` parsing
- `tests/tenant/test_tenant_ratelimiter_registry.py` — unit tests for registry + per-tenant isolation
- `tests/tenant/test_tenant_appservice.py` — unit tests for per-tenant AS loading and lookup

### Modified files
- `synapse/config/tenants.py:154-212` — add `TenantRatelimitConfig` dataclass + fields on `TenantConfig`
- `synapse/config/tenants.py:250-317` — update `from_dict` to parse ratelimit + app_service_config_files
- `synapse/server.py:709` — add `get_tenant_ratelimiter_registry()` singleton
- `synapse/handlers/room_member.py:134-192` — convert 6 limiters to tenant registry
- `synapse/rest/client/login.py:123-132` — convert 2 limiters
- `synapse/rest/client/register.py:404-408` — convert 1 limiter
- `synapse/rest/client/sync.py:135-139` — convert 1 limiter
- `synapse/rest/client/presence.py:55-59` — convert 1 limiter
- `synapse/handlers/identity.py:75-84` — convert 2 limiters
- `synapse/handlers/devicemessage.py:82-86` — convert 1 limiter
- `synapse/handlers/auth.py:224-238` — convert 2 limiters
- `synapse/rest/client/user_directory.py:50-54` — convert 1 limiter
- `synapse/rest/media/create_resource.py:51-55` — convert 1 limiter
- `synapse/media/media_repository.py:124-128` — convert 1 limiter
- `synapse/rest/client/login_token_request.py:81-88` — convert 1 limiter (hardcoded settings)
- `synapse/api/ratelimiting.py:392-420` — convert `RequestRatelimiter` to accept registry
- `synapse/storage/databases/main/appservice.py:75-117` — tenant-scoped AS loading and lookup

---

## Sub-phase 4a — Per-tenant rate limiting

### Task 1: TenantRatelimitConfig dataclass + red probes

**Files:**
- Modify: `synapse/config/tenants.py:154-212`
- Create: `tests/tenant/test_tenant_ratelimit_config.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/tenant/test_tenant_ratelimit_config.py`:

```python
#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

from twisted.trial import unittest

from synapse.config.tenants import TenantConfig, TenantRatelimitConfig


class TenantRatelimitConfigFromDictTestCase(unittest.TestCase):
    """Parse TenantRatelimitConfig from a YAML-style dict."""

    def test_full_config_round_trip(self):
        d = {
            "rc_message": {"per_second": 0.5, "burst_count": 20},
            "rc_login": {
                "address": {"per_second": 0.01, "burst_count": 10},
                "account": {"per_second": 0.01, "burst_count": 10},
            },
            "rc_joins": {
                "local": {"per_second": 0.2, "burst_count": 15},
            },
        }
        cfg = TenantRatelimitConfig.from_dict(d)
        self.assertIsNotNone(cfg.rc_message)
        self.assertAlmostEqual(cfg.rc_message.per_second, 0.5)
        self.assertEqual(cfg.rc_message.burst_count, 20)
        self.assertIsNotNone(cfg.rc_login_address)
        self.assertAlmostEqual(cfg.rc_login_address.per_second, 0.01)
        self.assertIsNotNone(cfg.rc_joins_local)
        self.assertAlmostEqual(cfg.rc_joins_local.per_second, 0.2)
        # Omitted keys are None (inherit global)
        self.assertIsNone(cfg.rc_registration)
        self.assertIsNone(cfg.rc_invites_per_room)

    def test_empty_dict_all_none(self):
        cfg = TenantRatelimitConfig.from_dict({})
        self.assertIsNone(cfg.rc_message)
        self.assertIsNone(cfg.rc_login_address)
        self.assertIsNone(cfg.rc_joins_local)

    def test_partial_login_only_address(self):
        d = {
            "rc_login": {
                "address": {"per_second": 0.05, "burst_count": 3},
            },
        }
        cfg = TenantRatelimitConfig.from_dict(d)
        self.assertIsNotNone(cfg.rc_login_address)
        self.assertAlmostEqual(cfg.rc_login_address.per_second, 0.05)
        # account not specified → None
        self.assertIsNone(cfg.rc_login_account)


class TenantConfigRatelimitFieldTestCase(unittest.TestCase):
    """TenantConfig.ratelimit field parsing via from_dict."""

    def test_ratelimit_present(self):
        cfg = TenantConfig.from_dict({
            "server_name": "acme.com",
            "signing_key_path": "/tmp/acme.key",
            "ratelimit": {
                "rc_message": {"per_second": 1.0, "burst_count": 50},
            },
        })
        self.assertIsNotNone(cfg.ratelimit)
        self.assertAlmostEqual(cfg.ratelimit.rc_message.per_second, 1.0)

    def test_ratelimit_absent_is_none(self):
        cfg = TenantConfig.from_dict({
            "server_name": "acme.com",
            "signing_key_path": "/tmp/acme.key",
        })
        self.assertIsNone(cfg.ratelimit)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 /tmp/synapse-venv/bin/trial tests.tenant.test_tenant_ratelimit_config
```

Expected: `ImportError` — `TenantRatelimitConfig` does not exist yet.

- [ ] **Step 3: Implement TenantRatelimitConfig**

In `synapse/config/tenants.py`, add after the `TenantPushConfig` class (after line 151) and before `TenantConfig`:

```python
@attr.s(auto_attribs=True, slots=True, frozen=True)
class TenantRatelimitConfig:
    """Per-tenant rate-limit settings.

    Each field is a RatelimitSettings or None. None means "inherit
    the global RatelimitConfig value for this limiter". This allows
    operators to override only the limiters they care about.
    """

    rc_message: "RatelimitSettings | None" = None
    rc_registration: "RatelimitSettings | None" = None
    rc_registration_token_validity: "RatelimitSettings | None" = None
    rc_login_address: "RatelimitSettings | None" = None
    rc_login_account: "RatelimitSettings | None" = None
    rc_login_failed_attempts: "RatelimitSettings | None" = None
    rc_joins_local: "RatelimitSettings | None" = None
    rc_joins_remote: "RatelimitSettings | None" = None
    rc_joins_per_room: "RatelimitSettings | None" = None
    rc_invites_per_room: "RatelimitSettings | None" = None
    rc_invites_per_user: "RatelimitSettings | None" = None
    rc_invites_per_issuer: "RatelimitSettings | None" = None
    rc_third_party_invite: "RatelimitSettings | None" = None
    rc_3pid_validation: "RatelimitSettings | None" = None
    rc_media_create: "RatelimitSettings | None" = None
    rc_presence_per_user: "RatelimitSettings | None" = None
    rc_user_directory: "RatelimitSettings | None" = None

    @classmethod
    def from_dict(cls, d: "JsonDict") -> "TenantRatelimitConfig":
        from synapse.config.ratelimiting import RatelimitSettings

        def _parse(key: str) -> "RatelimitSettings | None":
            """Parse a single rc_* key from the dict, returning None if absent."""
            rl_config = d
            for part in key.split("."):
                if not isinstance(rl_config, dict):
                    return None
                rl_config = rl_config.get(part)
                if rl_config is None:
                    return None
            if not isinstance(rl_config, dict):
                return None
            return RatelimitSettings(
                key=key,
                per_second=float(rl_config.get("per_second", 0.17)),
                burst_count=int(rl_config.get("burst_count", 3)),
            )

        return cls(
            rc_message=_parse("rc_message"),
            rc_registration=_parse("rc_registration"),
            rc_registration_token_validity=_parse("rc_registration_token_validity"),
            rc_login_address=_parse("rc_login.address"),
            rc_login_account=_parse("rc_login.account"),
            rc_login_failed_attempts=_parse("rc_login.failed_attempts"),
            rc_joins_local=_parse("rc_joins.local"),
            rc_joins_remote=_parse("rc_joins.remote"),
            rc_joins_per_room=_parse("rc_joins_per_room"),
            rc_invites_per_room=_parse("rc_invites.per_room"),
            rc_invites_per_user=_parse("rc_invites.per_user"),
            rc_invites_per_issuer=_parse("rc_invites.per_issuer"),
            rc_third_party_invite=_parse("rc_third_party_invite"),
            rc_3pid_validation=_parse("rc_3pid_validation"),
            rc_media_create=_parse("rc_media_create"),
            rc_presence_per_user=_parse("rc_presence.per_user"),
            rc_user_directory=_parse("rc_user_directory"),
        )
```

Add the `ratelimit` field to `TenantConfig` (after line 212, after `push`):

```python
    # Per-tenant rate-limit overrides. When set, individual limiters
    # override the corresponding global rc_* setting. When None, the
    # tenant inherits all global rate limits unchanged.
    ratelimit: TenantRatelimitConfig | None = None
```

Add `app_service_config_files` field too (for task 7):

```python
    # Per-tenant app service config file paths. When set, only these
    # AS registrations apply to this tenant. When None, no app services
    # are active for this tenant (AS namespaces are server_name-bound,
    # so global inheritance is not meaningful).
    app_service_config_files: list[str] | None = None
```

Update `TenantConfig.from_dict` (around line 296-317) to parse both new fields:

```python
        ratelimit_dict = config.get("ratelimit")
        ratelimit_cfg = (
            TenantRatelimitConfig.from_dict(ratelimit_dict)
            if ratelimit_dict
            else None
        )

        as_config_files = config.get("app_service_config_files")
```

And add to the `return cls(...)` call:

```python
            ratelimit=ratelimit_cfg,
            app_service_config_files=as_config_files,
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 /tmp/synapse-venv/bin/trial tests.tenant.test_tenant_ratelimit_config
```

Expected: All 5 tests pass.

- [ ] **Step 5: Run existing tenant config tests for regression**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 /tmp/synapse-venv/bin/trial tests.tenant.test_config
```

Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add synapse/config/tenants.py tests/tenant/test_tenant_ratelimit_config.py
git commit -m "feat(tenant): add TenantRatelimitConfig dataclass with per-field inheritance"
```

---

### Task 2: TenantRatelimiterRegistry + red probes

**Files:**
- Create: `synapse/api/tenant_ratelimiting.py`
- Modify: `synapse/server.py:709`
- Create: `tests/tenant/test_tenant_ratelimiter_registry.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/tenant/test_tenant_ratelimiter_registry.py`:

```python
#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

from unittest.mock import Mock

from twisted.trial import unittest

from synapse.api.ratelimiting import Ratelimiter
from synapse.config.ratelimiting import RatelimitSettings
from synapse.config.tenants import TenantConfig, TenantRatelimitConfig


class TenantRatelimiterRegistryTestCase(unittest.TestCase):
    """TenantRatelimiterRegistry returns per-tenant Ratelimiter instances."""

    def _make_registry(self):
        from synapse.api.tenant_ratelimiting import TenantRatelimiterRegistry

        store = Mock()
        store.get_ratelimit_for_user = Mock(return_value=None)
        clock = Mock()
        clock.time.return_value = 0.0
        clock.looping_call = Mock()

        # Global config with known defaults
        global_settings = {
            "rc_message": RatelimitSettings(
                key="rc_message", per_second=0.2, burst_count=10
            ),
        }
        return TenantRatelimiterRegistry(store, clock, global_settings)

    def test_no_tenant_returns_global_limiter(self):
        registry = self._make_registry()
        limiter = registry.get("rc_message", tenant=None)
        self.assertIsInstance(limiter, Ratelimiter)
        self.assertAlmostEqual(limiter.rate_hz, 0.2)
        self.assertEqual(limiter.burst_count, 10)

    def test_tenant_without_ratelimit_returns_global(self):
        registry = self._make_registry()
        tenant = TenantConfig(
            server_name="acme.com",
            database_schema="tenant_acme",
            signing_key_path="/tmp/acme.key",
            media_store_path="/tmp/media/acme",
            ratelimit=None,
        )
        limiter = registry.get("rc_message", tenant=tenant)
        self.assertAlmostEqual(limiter.rate_hz, 0.2)

    def test_tenant_with_override_returns_tenant_limiter(self):
        registry = self._make_registry()
        tenant = TenantConfig(
            server_name="corp.com",
            database_schema="tenant_corp",
            signing_key_path="/tmp/corp.key",
            media_store_path="/tmp/media/corp",
            ratelimit=TenantRatelimitConfig(
                rc_message=RatelimitSettings(
                    key="rc_message", per_second=1.0, burst_count=50
                ),
            ),
        )
        limiter = registry.get("rc_message", tenant=tenant)
        self.assertAlmostEqual(limiter.rate_hz, 1.0)
        self.assertEqual(limiter.burst_count, 50)

    def test_tenant_override_for_one_key_global_for_another(self):
        registry = self._make_registry()
        tenant = TenantConfig(
            server_name="corp.com",
            database_schema="tenant_corp",
            signing_key_path="/tmp/corp.key",
            media_store_path="/tmp/media/corp",
            ratelimit=TenantRatelimitConfig(
                rc_message=RatelimitSettings(
                    key="rc_message", per_second=5.0, burst_count=100
                ),
                # rc_joins_local not set → should get global
            ),
        )
        msg_limiter = registry.get("rc_message", tenant=tenant)
        self.assertAlmostEqual(msg_limiter.rate_hz, 5.0)

    def test_same_tenant_same_key_returns_cached_instance(self):
        registry = self._make_registry()
        tenant = TenantConfig(
            server_name="acme.com",
            database_schema="tenant_acme",
            signing_key_path="/tmp/acme.key",
            media_store_path="/tmp/media/acme",
        )
        limiter1 = registry.get("rc_message", tenant=tenant)
        limiter2 = registry.get("rc_message", tenant=tenant)
        self.assertIs(limiter1, limiter2)

    def test_different_tenants_get_different_instances(self):
        registry = self._make_registry()
        tenant_a = TenantConfig(
            server_name="acme.com",
            database_schema="tenant_acme",
            signing_key_path="/tmp/acme.key",
            media_store_path="/tmp/media/acme",
            ratelimit=TenantRatelimitConfig(
                rc_message=RatelimitSettings(
                    key="rc_message", per_second=0.5, burst_count=20
                ),
            ),
        )
        tenant_b = TenantConfig(
            server_name="corp.com",
            database_schema="tenant_corp",
            signing_key_path="/tmp/corp.key",
            media_store_path="/tmp/media/corp",
            ratelimit=TenantRatelimitConfig(
                rc_message=RatelimitSettings(
                    key="rc_message", per_second=2.0, burst_count=80
                ),
            ),
        )
        limiter_a = registry.get("rc_message", tenant=tenant_a)
        limiter_b = registry.get("rc_message", tenant=tenant_b)
        self.assertIsNot(limiter_a, limiter_b)
        self.assertAlmostEqual(limiter_a.rate_hz, 0.5)
        self.assertAlmostEqual(limiter_b.rate_hz, 2.0)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 /tmp/synapse-venv/bin/trial tests.tenant.test_tenant_ratelimiter_registry
```

Expected: `ModuleNotFoundError` — `synapse.api.tenant_ratelimiting` does not exist.

- [ ] **Step 3: Implement TenantRatelimiterRegistry**

Create `synapse/api/tenant_ratelimiting.py`:

```python
#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

"""
Per-tenant rate limiter registry.

Caches Ratelimiter instances keyed by (server_name, limiter_key).
When a tenant has a per-field override in TenantRatelimitConfig, a
tenant-specific Ratelimiter is created with those settings. Otherwise
the global Ratelimiter (shared across all tenants without overrides)
is returned.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from synapse.api.ratelimiting import Ratelimiter
from synapse.config.ratelimiting import RatelimitSettings

if TYPE_CHECKING:
    from synapse.config.tenants import TenantConfig
    from synapse.storage.databases.main import DataStore
    from synapse.util import Clock

logger = logging.getLogger(__name__)

# Sentinel for the "global" (no-tenant) cache key
_GLOBAL = "__global__"


class TenantRatelimiterRegistry:
    """Caches per-tenant Ratelimiter instances, creating them lazily.

    Args:
        store: The main data store (needed by Ratelimiter for override lookups).
        clock: The homeserver clock (needed by Ratelimiter for timing).
        global_settings: A dict mapping limiter key names (e.g. "rc_message")
            to the global RatelimitSettings for that limiter. These are used
            as defaults when a tenant doesn't override a particular limiter.
    """

    def __init__(
        self,
        store: "DataStore",
        clock: "Clock",
        global_settings: dict[str, RatelimitSettings],
    ) -> None:
        self._store = store
        self._clock = clock
        self._global_settings = global_settings
        # (server_name_or_GLOBAL, limiter_key) -> Ratelimiter
        self._cache: dict[tuple[str, str], Ratelimiter] = {}

    def get(
        self, limiter_key: str, tenant: "TenantConfig | None" = None
    ) -> Ratelimiter:
        """Return the Ratelimiter for the given limiter key and tenant.

        Falls back to global config when tenant is None, tenant.ratelimit
        is None, or the specific limiter_key is not overridden on the tenant.
        """
        # Resolve the settings to use
        tenant_setting = None
        if tenant is not None and tenant.ratelimit is not None:
            tenant_setting = getattr(tenant.ratelimit, limiter_key, None)

        if tenant_setting is not None:
            # Tenant has an override for this limiter
            cache_key = (tenant.server_name, limiter_key)
            settings = tenant_setting
        else:
            # Use global
            cache_key = (_GLOBAL, limiter_key)
            settings = self._global_settings.get(limiter_key)
            if settings is None:
                raise KeyError(
                    f"No global RatelimitSettings for limiter key {limiter_key!r}"
                )

        if cache_key not in self._cache:
            self._cache[cache_key] = Ratelimiter(
                store=self._store,
                clock=self._clock,
                cfg=settings,
            )

        return self._cache[cache_key]
```

- [ ] **Step 4: Register on HomeServer**

In `synapse/server.py`, add a new method after `get_tenant_registry` (around line 720):

```python
    @cache_in_self
    def get_tenant_ratelimiter_registry(
        self,
    ) -> "TenantRatelimiterRegistry":
        from synapse.api.tenant_ratelimiting import TenantRatelimiterRegistry

        rl = self.config.ratelimiting
        global_settings: dict[str, "RatelimitSettings"] = {
            "rc_message": rl.rc_message,
            "rc_registration": rl.rc_registration,
            "rc_registration_token_validity": rl.rc_registration_token_validity,
            "rc_login_address": rl.rc_login_address,
            "rc_login_account": rl.rc_login_account,
            "rc_login_failed_attempts": rl.rc_login_failed_attempts,
            "rc_joins_local": rl.rc_joins_local,
            "rc_joins_remote": rl.rc_joins_remote,
            "rc_joins_per_room": rl.rc_joins_per_room,
            "rc_invites_per_room": rl.rc_invites_per_room,
            "rc_invites_per_user": rl.rc_invites_per_user,
            "rc_invites_per_issuer": rl.rc_invites_per_issuer,
            "rc_third_party_invite": rl.rc_third_party_invite,
            "rc_3pid_validation": rl.rc_3pid_validation,
            "rc_media_create": rl.rc_media_create,
            "rc_presence_per_user": rl.rc_presence_per_user,
            "rc_user_directory": rl.rc_user_directory,
            "rc_key_requests": rl.rc_key_requests,
            "remote_media_downloads": rl.remote_media_downloads,
        }
        return TenantRatelimiterRegistry(
            store=self.get_datastores().main,
            clock=self.get_clock(),
            global_settings=global_settings,
        )
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 /tmp/synapse-venv/bin/trial tests.tenant.test_tenant_ratelimiter_registry
```

Expected: All 6 tests pass.

- [ ] **Step 6: Commit**

```bash
git add synapse/api/tenant_ratelimiting.py synapse/server.py tests/tenant/test_tenant_ratelimiter_registry.py
git commit -m "feat(tenant): add TenantRatelimiterRegistry for per-tenant rate limiter instances"
```

---

### Task 3: Convert room_member.py rate limiters (6 limiters)

**Files:**
- Modify: `synapse/handlers/room_member.py:134-192`

- [ ] **Step 1: Replace __init__ limiter creation**

In `synapse/handlers/room_member.py`, replace the 6 `Ratelimiter(...)` instantiations (lines 134-192) with a single registry reference. Replace:

```python
        self._join_rate_limiter_local = Ratelimiter(
            store=self.store,
            clock=self.clock,
            cfg=hs.config.ratelimiting.rc_joins_local,
        )
```

(and the other 5 similar blocks) with:

```python
        self._tenant_rl_registry = hs.get_tenant_ratelimiter_registry()
```

- [ ] **Step 2: Update all usage sites to resolve from registry**

Find every `self._join_rate_limiter_local`, `self._join_rate_limiter_remote`, `self._join_rate_per_room_limiter`, `self._invites_per_room_limiter`, `self._invites_per_recipient_limiter`, `self._invites_per_issuer_limiter`, `self._third_party_invite_limiter` in the file and replace with tenant-context-aware resolution. For each usage, the pattern is:

```python
# Before:
await self._join_rate_limiter_local.ratelimit(requester)

# After:
from synapse.tenant_context import get_current_tenant
limiter = self._tenant_rl_registry.get("rc_joins_local", get_current_tenant())
await limiter.ratelimit(requester)
```

Add the import at the top of the file:

```python
from synapse.tenant_context import get_current_tenant
```

Apply the same pattern for each limiter:
- `self._join_rate_limiter_local` → `self._tenant_rl_registry.get("rc_joins_local", get_current_tenant())`
- `self._join_rate_limiter_remote` → `self._tenant_rl_registry.get("rc_joins_remote", get_current_tenant())`
- `self._join_rate_per_room_limiter` → `self._tenant_rl_registry.get("rc_joins_per_room", get_current_tenant())`
- `self._invites_per_room_limiter` → `self._tenant_rl_registry.get("rc_invites_per_room", get_current_tenant())`
- `self._invites_per_recipient_limiter` → `self._tenant_rl_registry.get("rc_invites_per_user", get_current_tenant())`
- `self._invites_per_issuer_limiter` → `self._tenant_rl_registry.get("rc_invites_per_issuer", get_current_tenant())`
- `self._third_party_invite_limiter` → `self._tenant_rl_registry.get("rc_third_party_invite", get_current_tenant())`

Note: for limiters that pass `ratelimit_callbacks`, the `Ratelimiter` constructor accepts that param. The registry doesn't pass callbacks currently. Check whether `ratelimit_callbacks` is needed — `_invites_per_room_limiter` and `_invites_per_recipient_limiter` use it. These limiters need callbacks injected after retrieval or the registry needs a `callbacks` parameter. **Simplest fix:** add an optional `ratelimit_callbacks` override to `TenantRatelimiterRegistry.get()` that gets passed to `Ratelimiter.__init__`.

- [ ] **Step 3: Run existing tests**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 /tmp/synapse-venv/bin/trial tests.handlers.test_room_member
```

Expected: All pass (no tenant context = global limiter).

- [ ] **Step 4: Commit**

```bash
git add synapse/handlers/room_member.py
git commit -m "feat(tenant): room_member rate limiters resolve from tenant context"
```

---

### Task 4: Convert login.py rate limiters (2 limiters)

**Files:**
- Modify: `synapse/rest/client/login.py:123-132`

- [ ] **Step 1: Replace __init__ limiter creation with registry**

Replace:
```python
        self._address_ratelimiter = Ratelimiter(
            store=self._main_store,
            clock=hs.get_clock(),
            cfg=self.hs.config.ratelimiting.rc_login_address,
        )
        self._account_ratelimiter = Ratelimiter(
            store=self._main_store,
            clock=hs.get_clock(),
            cfg=self.hs.config.ratelimiting.rc_login_account,
        )
```

With:
```python
        self._tenant_rl_registry = hs.get_tenant_ratelimiter_registry()
```

- [ ] **Step 2: Update usage sites**

Find all uses of `self._address_ratelimiter` and `self._account_ratelimiter` in the file and replace:

```python
# Before:
await self._address_ratelimiter.ratelimit(...)

# After:
from synapse.tenant_context import get_current_tenant
await self._tenant_rl_registry.get(
    "rc_login_address", get_current_tenant()
).ratelimit(...)
```

Same for `self._account_ratelimiter` → `"rc_login_account"`.

- [ ] **Step 3: Run existing tests**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 /tmp/synapse-venv/bin/trial tests.rest.client.test_login
```

Expected: All pass.

- [ ] **Step 4: Commit**

```bash
git add synapse/rest/client/login.py
git commit -m "feat(tenant): login rate limiters resolve from tenant context"
```

---

### Task 5: Convert auth.py rate limiters (2 limiters)

**Files:**
- Modify: `synapse/handlers/auth.py:224-238`

- [ ] **Step 1: Replace __init__ limiter creation with registry**

Replace:
```python
        self._failed_uia_attempts_ratelimiter = Ratelimiter(
            store=self.store,
            clock=self.clock,
            cfg=self.hs.config.ratelimiting.rc_login_failed_attempts,
        )
        ...
        self._failed_login_attempts_ratelimiter = Ratelimiter(
            store=self.store,
            clock=hs.get_clock(),
            cfg=self.hs.config.ratelimiting.rc_login_failed_attempts,
        )
```

With:
```python
        self._tenant_rl_registry = hs.get_tenant_ratelimiter_registry()
```

- [ ] **Step 2: Update usage sites**

Replace `self._failed_uia_attempts_ratelimiter` and `self._failed_login_attempts_ratelimiter` usages:

```python
# Before:
await self._failed_uia_attempts_ratelimiter.ratelimit(...)

# After:
from synapse.tenant_context import get_current_tenant
await self._tenant_rl_registry.get(
    "rc_login_failed_attempts", get_current_tenant()
).ratelimit(...)
```

- [ ] **Step 3: Run existing tests**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 /tmp/synapse-venv/bin/trial tests.handlers.test_auth
```

Expected: All pass.

- [ ] **Step 4: Commit**

```bash
git add synapse/handlers/auth.py
git commit -m "feat(tenant): auth rate limiters resolve from tenant context"
```

---

### Task 6: Convert remaining handler limiters (identity, devicemessage)

**Files:**
- Modify: `synapse/handlers/identity.py:75-84`
- Modify: `synapse/handlers/devicemessage.py:82-86`

- [ ] **Step 1: Convert identity.py**

Replace the two `Ratelimiter` instantiations at lines 75-84:
```python
        self._3pid_validation_ratelimiter_ip = Ratelimiter(
            store=self.store,
            clock=hs.get_clock(),
            cfg=hs.config.ratelimiting.rc_3pid_validation,
        )
        self._3pid_validation_ratelimiter_address = Ratelimiter(
            store=self.store,
            clock=hs.get_clock(),
            cfg=hs.config.ratelimiting.rc_3pid_validation,
        )
```

With:
```python
        self._tenant_rl_registry = hs.get_tenant_ratelimiter_registry()
```

Update usages — both use `"rc_3pid_validation"` but they track different keys (IP vs address). Since they share the same `RatelimitSettings` but have different bucket keys, they need separate `Ratelimiter` instances. The registry caches by `(server_name, limiter_key)`, so both would return the same instance. This is actually fine — the `Ratelimiter` tracks per-key buckets internally (the `key` param to `can_do_action`), so one instance can track both IP and address keys independently.

```python
# Before:
await self._3pid_validation_ratelimiter_ip.ratelimit(None, (ip_address,))
await self._3pid_validation_ratelimiter_address.ratelimit(None, (medium, address))

# After:
from synapse.tenant_context import get_current_tenant
_tenant = get_current_tenant()
await self._tenant_rl_registry.get("rc_3pid_validation", _tenant).ratelimit(
    None, (ip_address,)
)
await self._tenant_rl_registry.get("rc_3pid_validation", _tenant).ratelimit(
    None, (medium, address)
)
```

The original code used two separate `Ratelimiter` instances, but since `Ratelimiter.actions` is keyed by the arbitrary `key` param passed to `can_do_action`/`ratelimit`, and the callers pass different key shapes (`(ip,)` vs `(medium, address)`), a single shared instance is safe — the keys won't collide. One registry lookup with `"rc_3pid_validation"` serves both call sites.

- [ ] **Step 2: Convert devicemessage.py**

Replace line 82-86:
```python
        self._ratelimiter = Ratelimiter(
            store=self.store,
            clock=hs.get_clock(),
            cfg=hs.config.ratelimiting.rc_key_requests,
        )
```

With:
```python
        self._tenant_rl_registry = hs.get_tenant_ratelimiter_registry()
```

Update usage:
```python
# Before:
await self._ratelimiter.can_do_action(...)

# After:
from synapse.tenant_context import get_current_tenant
limiter = self._tenant_rl_registry.get("rc_key_requests", get_current_tenant())
await limiter.can_do_action(...)
```

- [ ] **Step 3: Run existing tests**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 /tmp/synapse-venv/bin/trial tests.handlers.test_device tests.handlers.test_identity
```

Expected: All pass.

- [ ] **Step 4: Commit**

```bash
git add synapse/handlers/identity.py synapse/handlers/devicemessage.py
git commit -m "feat(tenant): identity and devicemessage rate limiters resolve from tenant context"
```

---

### Task 7: Convert REST client limiters (register, sync, presence, user_directory, login_token_request)

**Files:**
- Modify: `synapse/rest/client/register.py:404-408`
- Modify: `synapse/rest/client/sync.py:135-139`
- Modify: `synapse/rest/client/presence.py:55-59`
- Modify: `synapse/rest/client/user_directory.py:50-54`
- Modify: `synapse/rest/client/login_token_request.py:81-88`

- [ ] **Step 1: Convert register.py**

Replace:
```python
        self.ratelimiter = Ratelimiter(
            store=self.store,
            clock=hs.get_clock(),
            cfg=hs.config.ratelimiting.rc_registration_token_validity,
        )
```

With:
```python
        self._tenant_rl_registry = hs.get_tenant_ratelimiter_registry()
```

Update usage (line 411):
```python
# Before:
await self.ratelimiter.ratelimit(None, (request.getClientAddress().host,))

# After:
from synapse.tenant_context import get_current_tenant
await self._tenant_rl_registry.get(
    "rc_registration_token_validity", get_current_tenant()
).ratelimit(None, (request.getClientAddress().host,))
```

- [ ] **Step 2: Convert sync.py**

Replace:
```python
        self._presence_per_user_limiter = Ratelimiter(
            store=self.store,
            clock=self.clock,
            cfg=hs.config.ratelimiting.rc_presence_per_user,
        )
```

With:
```python
        self._tenant_rl_registry = hs.get_tenant_ratelimiter_registry()
```

Update usage:
```python
from synapse.tenant_context import get_current_tenant
await self._tenant_rl_registry.get(
    "rc_presence_per_user", get_current_tenant()
).ratelimit(requester)
```

- [ ] **Step 3: Convert presence.py**

Same pattern as sync.py — replace `self._presence_per_user_limiter` with registry get.

- [ ] **Step 4: Convert user_directory.py**

Replace:
```python
        self._per_user_limiter = Ratelimiter(
            store=hs.get_datastores().main,
            clock=hs.get_clock(),
            cfg=hs.config.ratelimiting.rc_user_directory,
        )
```

With:
```python
        self._tenant_rl_registry = hs.get_tenant_ratelimiter_registry()
```

Update usage:
```python
from synapse.tenant_context import get_current_tenant
await self._tenant_rl_registry.get(
    "rc_user_directory", get_current_tenant()
).ratelimit(requester)
```

- [ ] **Step 5: Convert login_token_request.py**

This one uses a hardcoded `RatelimitSettings` (not from config). Replace:
```python
        self._ratelimiter = Ratelimiter(
            store=self._main_store,
            clock=hs.get_clock(),
            cfg=RatelimitSettings(
                key="<login token request>",
                per_second=1 / 60,
                burst_count=1,
            ),
        )
```

Since this limiter uses hardcoded settings (not from `RatelimitConfig`), keep it as-is — it doesn't need per-tenant overrides. The rate limit is a security bound, not a noisy-neighbor concern. **Skip this file.**

- [ ] **Step 6: Run existing tests**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 /tmp/synapse-venv/bin/trial tests.rest.client.test_register tests.rest.client.test_sync tests.rest.client.test_presence
```

Expected: All pass.

- [ ] **Step 7: Commit**

```bash
git add synapse/rest/client/register.py synapse/rest/client/sync.py synapse/rest/client/presence.py synapse/rest/client/user_directory.py
git commit -m "feat(tenant): REST client rate limiters resolve from tenant context"
```

---

### Task 8: Convert media limiters + RequestRatelimiter

**Files:**
- Modify: `synapse/rest/media/create_resource.py:51-55`
- Modify: `synapse/media/media_repository.py:124-128`
- Modify: `synapse/api/ratelimiting.py:392-420`

- [ ] **Step 1: Convert create_resource.py**

Replace:
```python
        self._create_media_rate_limiter = Ratelimiter(
            store=hs.get_datastores().main,
            clock=self.clock,
            cfg=hs.config.ratelimiting.rc_media_create,
        )
```

With:
```python
        self._tenant_rl_registry = hs.get_tenant_ratelimiter_registry()
```

Update usage:
```python
from synapse.tenant_context import get_current_tenant
await self._tenant_rl_registry.get(
    "rc_media_create", get_current_tenant()
).ratelimit(requester)
```

- [ ] **Step 2: Convert media_repository.py**

Replace:
```python
        self.download_ratelimiter = Ratelimiter(
            store=hs.get_storage_controllers().main,
            clock=hs.get_clock(),
            cfg=hs.config.ratelimiting.remote_media_downloads,
        )
```

With:
```python
        self._tenant_rl_registry = hs.get_tenant_ratelimiter_registry()
```

Update usages of `self.download_ratelimiter`:
```python
from synapse.tenant_context import get_current_tenant
limiter = self._tenant_rl_registry.get(
    "remote_media_downloads", get_current_tenant()
)
```

- [ ] **Step 3: Convert RequestRatelimiter**

`RequestRatelimiter` in `synapse/api/ratelimiting.py:392` is constructed with explicit `RatelimitSettings`. Modify its `__init__` to accept a `TenantRatelimiterRegistry` instead:

```python
class RequestRatelimiter:
    def __init__(
        self,
        store: DataStore,
        clock: Clock,
        rc_message: RatelimitSettings,
        rc_admin_redaction: RatelimitSettings | None,
        tenant_rl_registry: "TenantRatelimiterRegistry | None" = None,
    ):
        self.store = store
        self.clock = clock
        self._rc_message = rc_message
        self._tenant_rl_registry = tenant_rl_registry

        # The global request_ratelimiter is still needed for per-user override logic
        self.request_ratelimiter = Ratelimiter(
            store=self.store,
            clock=self.clock,
            cfg=RatelimitSettings(key=rc_message.key, per_second=0, burst_count=0),
        )

        if rc_admin_redaction:
            self.admin_redaction_ratelimiter: Ratelimiter | None = Ratelimiter(
                store=self.store,
                clock=self.clock,
                cfg=rc_admin_redaction,
            )
        else:
            self.admin_redaction_ratelimiter = None
```

In the `ratelimit` method, resolve `_rc_message` settings per-tenant when the registry is available:

```python
    async def ratelimit(self, requester, ...):
        ...
        if self._tenant_rl_registry is not None:
            from synapse.tenant_context import get_current_tenant
            tenant = get_current_tenant()
            if tenant and tenant.ratelimit and tenant.ratelimit.rc_message:
                messages_per_second = tenant.ratelimit.rc_message.per_second
                burst_count = tenant.ratelimit.rc_message.burst_count
```

- [ ] **Step 4: Run existing tests**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 /tmp/synapse-venv/bin/trial tests.api.test_ratelimiting tests.rest.media
```

Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add synapse/rest/media/create_resource.py synapse/media/media_repository.py synapse/api/ratelimiting.py
git commit -m "feat(tenant): media and RequestRatelimiter resolve from tenant context"
```

---

### Task 9: Run full 4a probe suite green

**Files:**
- Verify: `tests/tenant/test_tenant_ratelimit_config.py`
- Verify: `tests/tenant/test_tenant_ratelimiter_registry.py`

- [ ] **Step 1: Run all tenant tests**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 /tmp/synapse-venv/bin/trial tests.tenant
```

Expected: All pass (including pre-existing tests from phases 1-3).

- [ ] **Step 2: Run existing rate-limiting tests for regression**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 /tmp/synapse-venv/bin/trial tests.api.test_ratelimiting
```

Expected: All pass.

---

## Sub-phase 4b — Per-tenant app services

### Task 10: TenantAppServiceRegistry + red probes

**Files:**
- Create: `synapse/appservice/tenant_registry.py`
- Create: `tests/tenant/test_tenant_appservice.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/tenant/test_tenant_appservice.py`:

```python
#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

from unittest.mock import patch

from twisted.trial import unittest

from synapse.appservice import ApplicationService
from synapse.config.tenants import TenantConfig
from synapse.tenant_context import set_current_tenant


class TenantAppServiceRegistryTestCase(unittest.TestCase):
    """Per-tenant app service isolation."""

    def _make_registry(self):
        from synapse.appservice.tenant_registry import TenantAppServiceRegistry

        tenant_a = TenantConfig(
            server_name="acme.com",
            database_schema="tenant_acme",
            signing_key_path="/tmp/acme.key",
            media_store_path="/tmp/media/acme",
            app_service_config_files=["/etc/as/acme-bridge.yaml"],
        )
        tenant_b = TenantConfig(
            server_name="corp.com",
            database_schema="tenant_corp",
            signing_key_path="/tmp/corp.key",
            media_store_path="/tmp/media/corp",
            app_service_config_files=None,  # no AS
        )

        as_acme = ApplicationService(
            token="acme-token",
            id="acme-bridge",
            sender="@acme-bridge:acme.com",
            namespaces={
                "users": [{"regex": "@irc_.*:acme.com", "exclusive": True}],
                "aliases": [],
                "rooms": [],
            },
        )

        # Patch load_appservices to return our mock AS
        with patch(
            "synapse.appservice.tenant_registry.load_appservices"
        ) as mock_load:
            mock_load.side_effect = lambda hostname, files: (
                [as_acme] if hostname == "acme.com" else []
            )
            registry = TenantAppServiceRegistry([tenant_a, tenant_b])

        return registry, tenant_a, tenant_b, as_acme

    def test_tenant_with_as_returns_services(self):
        registry, tenant_a, _, as_acme = self._make_registry()
        services = registry.get_app_services(tenant_a)
        self.assertEqual(len(services), 1)
        self.assertEqual(services[0].id, "acme-bridge")

    def test_tenant_without_as_returns_empty(self):
        registry, _, tenant_b, _ = self._make_registry()
        services = registry.get_app_services(tenant_b)
        self.assertEqual(services, [])

    def test_get_by_user_id_in_correct_tenant(self):
        registry, tenant_a, _, as_acme = self._make_registry()
        result = registry.get_app_service_by_user_id(
            "@acme-bridge:acme.com", tenant_a
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.id, "acme-bridge")

    def test_get_by_user_id_wrong_tenant_returns_none(self):
        registry, _, tenant_b, _ = self._make_registry()
        result = registry.get_app_service_by_user_id(
            "@acme-bridge:acme.com", tenant_b
        )
        self.assertIsNone(result)

    def test_get_by_token_in_correct_tenant(self):
        registry, tenant_a, _, _ = self._make_registry()
        result = registry.get_app_service_by_token("acme-token", tenant_a)
        self.assertIsNotNone(result)

    def test_get_by_token_wrong_tenant_returns_none(self):
        registry, _, tenant_b, _ = self._make_registry()
        result = registry.get_app_service_by_token("acme-token", tenant_b)
        self.assertIsNone(result)

    def test_get_all_services_returns_union(self):
        registry, _, _, _ = self._make_registry()
        all_services = registry.get_all_app_services()
        self.assertEqual(len(all_services), 1)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 /tmp/synapse-venv/bin/trial tests.tenant.test_tenant_appservice
```

Expected: `ModuleNotFoundError` — `synapse.appservice.tenant_registry` does not exist.

- [ ] **Step 3: Implement TenantAppServiceRegistry**

Create `synapse/appservice/tenant_registry.py`:

```python
#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

"""
Per-tenant app service registry.

Loads and caches ApplicationService objects per tenant at startup.
Provides tenant-scoped lookup methods that parallel the global
methods on ApplicationServiceWorkerStore.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from synapse.appservice import ApplicationService
from synapse.config.appservice import load_appservices

if TYPE_CHECKING:
    from synapse.config.tenants import TenantConfig

logger = logging.getLogger(__name__)


def _make_exclusive_regex(
    services: list[ApplicationService],
) -> re.Pattern[str] | None:
    """Compile a single regex matching all exclusive user namespaces."""
    patterns = []
    for service in services:
        for namespace in service.namespaces["users"]:
            if namespace.get("exclusive"):
                patterns.append(namespace["regex"])
    if not patterns:
        return None
    return re.compile("|".join("(?:%s)" % p for p in patterns))


class TenantAppServiceRegistry:
    """Holds per-tenant ApplicationService lists.

    Args:
        tenants: All configured TenantConfig instances.
    """

    def __init__(self, tenants: list["TenantConfig"]) -> None:
        self._tenant_services: dict[str, list[ApplicationService]] = {}
        self._tenant_exclusive_regex: dict[str, re.Pattern[str] | None] = {}
        self._all_services: list[ApplicationService] = []

        for tenant in tenants:
            files = tenant.app_service_config_files or []
            if files:
                services = load_appservices(tenant.server_name, files)
            else:
                services = []
            self._tenant_services[tenant.server_name] = services
            self._tenant_exclusive_regex[tenant.server_name] = (
                _make_exclusive_regex(services)
            )
            self._all_services.extend(services)

    def get_app_services(
        self, tenant: "TenantConfig | None" = None
    ) -> list[ApplicationService]:
        if tenant is None:
            return []
        return self._tenant_services.get(tenant.server_name, [])

    def get_app_service_by_user_id(
        self, user_id: str, tenant: "TenantConfig | None" = None
    ) -> ApplicationService | None:
        for service in self.get_app_services(tenant):
            if service.sender == user_id:
                return service
        return None

    def get_app_service_by_token(
        self, token: str, tenant: "TenantConfig | None" = None
    ) -> ApplicationService | None:
        for service in self.get_app_services(tenant):
            if service.token == token:
                return service
        return None

    def get_if_app_services_interested_in_user(
        self, user_id: str, tenant: "TenantConfig | None" = None
    ) -> bool:
        if tenant is None:
            return False
        regex = self._tenant_exclusive_regex.get(tenant.server_name)
        if regex:
            return bool(regex.match(user_id))
        return False

    def get_all_app_services(self) -> list[ApplicationService]:
        """Return the union of all tenant AS lists (for admin/recovery)."""
        return list(self._all_services)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 /tmp/synapse-venv/bin/trial tests.tenant.test_tenant_appservice
```

Expected: All 7 tests pass.

- [ ] **Step 5: Commit**

```bash
git add synapse/appservice/tenant_registry.py tests/tenant/test_tenant_appservice.py
git commit -m "feat(tenant): add TenantAppServiceRegistry for per-tenant AS isolation"
```

---

### Task 11: Register TenantAppServiceRegistry on HomeServer + wire into store

**Files:**
- Modify: `synapse/server.py`
- Modify: `synapse/storage/databases/main/appservice.py:75-117`

- [ ] **Step 1: Add HomeServer singleton**

In `synapse/server.py`, add after `get_tenant_ratelimiter_registry`:

```python
    @cache_in_self
    def get_tenant_app_service_registry(
        self,
    ) -> "TenantAppServiceRegistry":
        from synapse.appservice.tenant_registry import TenantAppServiceRegistry

        registry = self.get_tenant_registry()
        tenants = list(registry.tenants.values()) if registry else []
        return TenantAppServiceRegistry(tenants)
```

- [ ] **Step 2: Make store methods tenant-aware**

In `synapse/storage/databases/main/appservice.py`, modify `ApplicationServiceWorkerStore`:

Add the tenant-aware dispatch in `get_app_services`:

```python
    def get_app_services(self) -> list[ApplicationService]:
        from synapse.tenant_context import get_current_tenant

        tenant = get_current_tenant()
        if tenant is not None:
            # Import here to avoid circular imports at module level
            from synapse.appservice.tenant_registry import TenantAppServiceRegistry
            # Access the registry from the hs instance if available
            if hasattr(self, '_tenant_as_registry'):
                return self._tenant_as_registry.get_app_services(tenant)
        return self.services_cache
```

Similarly for `get_app_service_by_user_id` and `get_app_service_by_token`.

A cleaner approach: inject the `TenantAppServiceRegistry` into the store at startup. In `ApplicationServiceWorkerStore.__init__`, after loading the global cache, also initialize the tenant registry reference:

```python
        # Will be set by HomeServer after full initialization
        self._tenant_as_registry: "TenantAppServiceRegistry | None" = None
```

And in `HomeServer._setup()` or equivalent, wire it:
```python
        store = self.get_datastores().main
        store._tenant_as_registry = self.get_tenant_app_service_registry()
```

- [ ] **Step 3: Run existing AS tests**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 /tmp/synapse-venv/bin/trial tests.handlers.test_appservice
```

Expected: All pass (tests mock the store, so tenant-aware dispatch doesn't affect them).

- [ ] **Step 4: Commit**

```bash
git add synapse/server.py synapse/storage/databases/main/appservice.py
git commit -m "feat(tenant): wire TenantAppServiceRegistry into HomeServer and AS store"
```

---

### Task 12: Run full 4b probe suite green

**Files:**
- Verify: `tests/tenant/test_tenant_appservice.py`

- [ ] **Step 1: Run all tenant tests**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 /tmp/synapse-venv/bin/trial tests.tenant
```

Expected: All pass.

- [ ] **Step 2: Run existing appservice tests for regression**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 /tmp/synapse-venv/bin/trial tests.handlers.test_appservice tests.appservice.test_scheduler
```

Expected: All pass.

---

### Task 13: Full regression + sync roadmap

- [ ] **Step 1: Run full tenant test suite**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 /tmp/synapse-venv/bin/trial tests.tenant
```

Expected: All pass.

- [ ] **Step 2: Run full rate-limiting + appservice regression**

```bash
SYNAPSE_SKIP_RUST_CHECK=1 /tmp/synapse-venv/bin/trial tests.api.test_ratelimiting tests.handlers.test_appservice tests.appservice
```

Expected: All pass.

- [ ] **Step 3: Run /sync-roadmap**

Update `roadmap-progess.md` and `multi-tenancy-workflow/data.js` to reflect Phase 4 completion.
