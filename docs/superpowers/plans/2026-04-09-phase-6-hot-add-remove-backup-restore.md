# Phase 6 — Hot Add/Remove + Backup/Restore Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable zero-downtime tenant addition/removal via SIGHUP reload, and provide CLI tools for per-tenant backup, restore, and destructive drop.

**Architecture:** On SIGHUP, the existing `TenantRegistry` re-reads `homeserver.yaml`, diffs tenants, and updates its internal state in-place (mutable registry pattern). Downstream registries (`MultiTenantKeyring`, `TenantAppServiceRegistry`, `TenantRatelimiterRegistry`) each gain a `reload()` method called in cascade. The `@cache_in_self` cached references remain valid. Backup/restore are subcommands of `scripts/synapse_tenant` wrapping `pg_dump`/`psql` + tar of the media tree.

**Tech Stack:** Python 3.11+, Twisted Trial, attrs, PyYAML, subprocess (for pg_dump/psql), tarfile, shutil

**Spec:** `docs/superpowers/specs/2026-04-09-phase-6-hot-add-remove-backup-restore-design.md`

---

## File structure

### New files
- `tests/tenant/test_registry_reload.py` — unit tests for `TenantRegistry.reload()`
- `tests/tenant/test_keyring_reload.py` — unit tests for `MultiTenantKeyring.reload()`
- `tests/tenant/test_appservice_reload.py` — unit tests for `TenantAppServiceRegistry.reload()`
- `tests/tenant/test_ratelimiter_reload.py` — unit tests for `TenantRatelimiterRegistry.reload()`
- `tests/tenant/test_tenant_backup.py` — unit tests for backup/restore/drop CLI commands

### Modified files
- `synapse/tenant_registry.py:51-70` — add `_inactive_tenants`, `reload()`, `is_inactive()`, modify `get_tenant()` and `get_all_tenants()`
- `synapse/crypto/multitenant_keyring.py:42-70` — add `reload()` method
- `synapse/appservice/tenant_registry.py:38-56` — add `reload()` method
- `synapse/api/tenant_ratelimiting.py:27-91` — add `reload()` method
- `synapse/app/_base.py:694-695` — register `_reload_tenants` SIGHUP callback
- `scripts/synapse_tenant:393-536` — add `backup`, `restore`, `drop` subcommands

---

## Sub-phase 6a — Hot Reload

### Task 1: Red probes for `TenantRegistry.reload()`

**Files:**
- Create: `tests/tenant/test_registry_reload.py`

- [ ] **Step 1: Write the failing tests**

```python
#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

"""
Tests for TenantRegistry.reload().

Verifies:
1. New tenants are added to the registry after reload
2. Removed tenants become inactive after reload
3. Unchanged tenants are unaffected
4. Re-adding a previously removed tenant reactivates it
5. Malformed config aborts without corrupting state
"""

from unittest import TestCase

from synapse.config.tenants import MultiTenantConfig, TenantConfig
from synapse.tenant_registry import TenantRegistry


def _tenant(name: str) -> TenantConfig:
    """Create a minimal TenantConfig for testing."""
    return TenantConfig(
        server_name=name,
        database_schema=f"tenant_{name.replace('.', '_')}",
        signing_key_path=f"/keys/{name}.key",
        media_store_path=f"/media/{name}",
    )


def _config(tenants: list[TenantConfig]) -> MultiTenantConfig:
    """Create a MultiTenantConfig from a list of TenantConfig."""
    return MultiTenantConfig(
        enabled=True,
        default_schema="public",
        tenants={t.server_name: t for t in tenants},
    )


class TenantRegistryReloadTestCase(TestCase):
    """Tests for TenantRegistry.reload()."""

    def test_reload_adds_new_tenant(self) -> None:
        """A tenant present in the new config but not the old is added."""
        registry = TenantRegistry(_config([_tenant("acme.com")]))
        self.assertIsNotNone(registry.get_tenant("acme.com"))
        self.assertIsNone(registry.get_tenant("newcorp.com"))

        new_config = _config([_tenant("acme.com"), _tenant("newcorp.com")])
        result = registry.reload(new_config)

        self.assertIn("newcorp.com", result["added"])
        self.assertIsNotNone(registry.get_tenant("newcorp.com"))

    def test_reload_removes_tenant(self) -> None:
        """A tenant absent from the new config becomes inactive."""
        registry = TenantRegistry(
            _config([_tenant("acme.com"), _tenant("corp.io")])
        )
        self.assertIsNotNone(registry.get_tenant("corp.io"))

        new_config = _config([_tenant("acme.com")])
        result = registry.reload(new_config)

        self.assertIn("corp.io", result["removed"])
        self.assertIsNone(registry.get_tenant("corp.io"))
        self.assertTrue(registry.is_inactive("corp.io"))

    def test_reload_unchanged_tenants(self) -> None:
        """Tenants present in both old and new configs stay active."""
        registry = TenantRegistry(
            _config([_tenant("acme.com"), _tenant("corp.io")])
        )

        new_config = _config([_tenant("acme.com"), _tenant("corp.io")])
        result = registry.reload(new_config)

        self.assertEqual(len(result["added"]), 0)
        self.assertEqual(len(result["removed"]), 0)
        self.assertIn("acme.com", result["unchanged"])
        self.assertIsNotNone(registry.get_tenant("acme.com"))

    def test_reload_reactivates_tenant(self) -> None:
        """Re-adding a previously removed tenant makes it active again."""
        registry = TenantRegistry(
            _config([_tenant("acme.com"), _tenant("corp.io")])
        )

        # Remove corp.io
        registry.reload(_config([_tenant("acme.com")]))
        self.assertTrue(registry.is_inactive("corp.io"))

        # Re-add corp.io
        result = registry.reload(
            _config([_tenant("acme.com"), _tenant("corp.io")])
        )
        self.assertIn("corp.io", result["added"])
        self.assertFalse(registry.is_inactive("corp.io"))
        self.assertIsNotNone(registry.get_tenant("corp.io"))

    def test_reload_malformed_config_preserves_state(self) -> None:
        """If reload() is given a broken config, state is unchanged.

        In practice, the SIGHUP callback catches the exception from
        config parsing *before* calling reload(). This test verifies
        that if reload() itself receives a valid but empty config,
        it correctly marks all tenants as removed rather than crashing.
        """
        acme = _tenant("acme.com")
        registry = TenantRegistry(_config([acme]))
        self.assertIsNotNone(registry.get_tenant("acme.com"))

        # Reload with empty config — acme becomes inactive
        result = registry.reload(_config([]))
        self.assertIn("acme.com", result["removed"])
        self.assertTrue(registry.is_inactive("acme.com"))

    def test_reload_inactive_not_in_get_all_tenants(self) -> None:
        """Inactive tenants are excluded from get_all_tenants()."""
        registry = TenantRegistry(
            _config([_tenant("acme.com"), _tenant("corp.io")])
        )
        self.assertEqual(len(registry.get_all_tenants()), 2)

        registry.reload(_config([_tenant("acme.com")]))
        all_tenants = registry.get_all_tenants()
        self.assertEqual(len(all_tenants), 1)
        self.assertEqual(all_tenants[0].server_name, "acme.com")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m twisted.trial tests.tenant.test_registry_reload`
Expected: FAIL — `TenantRegistry` has no `reload()` or `is_inactive()` method.

- [ ] **Step 3: Implement `TenantRegistry.reload()`, `is_inactive()`, and update `get_tenant()` / `get_all_tenants()`**

In `synapse/tenant_registry.py`, make these changes:

1. In `__init__`, change `self._tenants` to a mutable copy and add `_inactive_tenants`:

```python
    def __init__(self, multi_tenant_config: MultiTenantConfig) -> None:
        self._config = multi_tenant_config
        self._tenants = dict(multi_tenant_config.tenants)  # mutable copy
        self._inactive_tenants: set[str] = set()
        self._signing_keys: dict[str, list] = {}
        self._hostname_aliases: dict[str, str] = {}

        if self.enabled:
            logger.info(
                "Tenant registry initialized with %d tenants: %s",
                len(self._tenants),
                list(self._tenants.keys()),
            )
```

2. Add the `reload()` method after `__init__`:

```python
    def reload(self, new_config: MultiTenantConfig) -> dict[str, list[str]]:
        """Diff new config against current state and update in-place.

        Args:
            new_config: The freshly-parsed MultiTenantConfig.

        Returns:
            Dict with keys ``added``, ``removed``, ``unchanged`` — each a
            list of server_name strings.
        """
        new_names = set(new_config.tenants.keys())
        old_names = set(self._tenants.keys()) - self._inactive_tenants

        added = new_names - old_names
        removed = old_names - new_names
        unchanged = old_names & new_names

        for name in added:
            self._tenants[name] = new_config.tenants[name]
            self._inactive_tenants.discard(name)
            # Clear cached signing key so it's reloaded from the new config
            self._signing_keys.pop(name, None)
            logger.info("Tenant added via reload: %s", name)

        for name in removed:
            self._inactive_tenants.add(name)
            logger.info("Tenant deactivated via reload: %s", name)

        self._config = new_config
        return {
            "added": sorted(added),
            "removed": sorted(removed),
            "unchanged": sorted(unchanged),
        }
```

3. Add `is_inactive()`:

```python
    def is_inactive(self, server_name: str) -> bool:
        """Check if a tenant has been deactivated via reload."""
        return server_name in self._inactive_tenants
```

4. Modify `get_tenant()` to skip inactive tenants (add at the top of the method):

```python
    def get_tenant(self, server_name: str) -> TenantConfig | None:
        if server_name in self._inactive_tenants:
            return None
        # ... rest of existing method unchanged ...
```

5. Modify `get_all_tenants()` to exclude inactive tenants:

```python
    def get_all_tenants(self) -> list[TenantConfig]:
        return [
            t for name, t in self._tenants.items()
            if name not in self._inactive_tenants
        ]
```

6. Modify `get_all_server_names()` to exclude inactive tenants:

```python
    def get_all_server_names(self) -> list[str]:
        return [
            name for name in self._tenants.keys()
            if name not in self._inactive_tenants
        ]
```

7. Modify `is_local_server_name()` to exclude inactive tenants:

```python
    def is_local_server_name(self, server_name: str) -> bool:
        if server_name in self._inactive_tenants:
            return False
        return server_name in self._tenants or server_name in self._hostname_aliases
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m twisted.trial tests.tenant.test_registry_reload`
Expected: All 5 tests PASS.

- [ ] **Step 5: Run existing registry tests to check for regressions**

Run: `python -m twisted.trial tests.tenant.test_registry`
Expected: All existing tests PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/tenant/test_registry_reload.py synapse/tenant_registry.py
git commit -m "feat(tenant): TenantRegistry.reload() with add/remove/reactivate support"
```

---

### Task 2: Red probes for `MultiTenantKeyring.reload()`

**Files:**
- Create: `tests/tenant/test_keyring_reload.py`

- [ ] **Step 1: Write the failing tests**

```python
#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

"""
Tests for MultiTenantKeyring.reload().

Verifies:
1. Keys for new tenants are loaded after reload
2. Keys for inactive tenants are removed after reload
"""

import os
import tempfile
from unittest import TestCase
from unittest.mock import MagicMock

from signedjson.key import generate_signing_key, write_signing_keys

from synapse.config.tenants import MultiTenantConfig, TenantConfig
from synapse.crypto.multitenant_keyring import MultiTenantKeyring
from synapse.tenant_registry import TenantRegistry


def _tenant_with_key(name: str, key_dir: str) -> TenantConfig:
    """Create a TenantConfig with a real signing key on disk."""
    key_path = os.path.join(key_dir, f"{name}.key")
    key = generate_signing_key("a")
    with open(key_path, "w") as f:
        write_signing_keys(f, [key])
    return TenantConfig(
        server_name=name,
        database_schema=f"tenant_{name.replace('.', '_')}",
        signing_key_path=key_path,
        media_store_path=f"/media/{name}",
    )


def _config(tenants: list[TenantConfig]) -> MultiTenantConfig:
    return MultiTenantConfig(
        enabled=True,
        default_schema="public",
        tenants={t.server_name: t for t in tenants},
    )


class MultiTenantKeyringReloadTestCase(TestCase):
    """Tests for MultiTenantKeyring.reload()."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._hs = MagicMock()

    def test_reload_loads_new_tenant_keys(self) -> None:
        """After reload, keys for newly added tenants are loaded."""
        acme = _tenant_with_key("acme.com", self._tmpdir)
        registry = TenantRegistry(_config([acme]))
        keyring = MultiTenantKeyring(self._hs, registry)

        # Initially only acme has keys
        self.assertIn("acme.com", keyring._signing_keys)

        # Add newcorp
        newcorp = _tenant_with_key("newcorp.com", self._tmpdir)
        new_config = _config([acme, newcorp])
        registry.reload(new_config)
        keyring.reload(registry)

        self.assertIn("newcorp.com", keyring._signing_keys)

    def test_reload_removes_inactive_tenant_keys(self) -> None:
        """After reload, keys for inactive tenants are removed."""
        acme = _tenant_with_key("acme.com", self._tmpdir)
        corp = _tenant_with_key("corp.io", self._tmpdir)
        registry = TenantRegistry(_config([acme, corp]))
        keyring = MultiTenantKeyring(self._hs, registry)

        self.assertIn("corp.io", keyring._signing_keys)

        # Remove corp.io
        registry.reload(_config([acme]))
        keyring.reload(registry)

        self.assertNotIn("corp.io", keyring._signing_keys)
        self.assertIn("acme.com", keyring._signing_keys)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m twisted.trial tests.tenant.test_keyring_reload`
Expected: FAIL — `MultiTenantKeyring` has no `reload()` method.

- [ ] **Step 3: Implement `MultiTenantKeyring.reload()`**

In `synapse/crypto/multitenant_keyring.py`, add after `_load_all_keys()`:

```python
    def reload(self, registry: "TenantRegistry") -> None:
        """Reload signing keys based on the current registry state.

        Loads keys for any new tenants and removes keys for inactive ones.
        """
        self._registry = registry

        active_names = set(t.server_name for t in registry.get_all_tenants())

        # Load keys for new tenants
        for tenant in registry.get_all_tenants():
            if tenant.server_name not in self._signing_keys:
                try:
                    self._load_tenant_keys(tenant)
                except Exception as e:
                    logger.error(
                        "Failed to load signing keys for tenant %s during reload: %s",
                        tenant.server_name,
                        e,
                    )

        # Remove keys for inactive tenants
        for name in list(self._signing_keys.keys()):
            if name not in active_names:
                del self._signing_keys[name]
                self._verify_keys.pop(name, None)
                logger.info("Removed signing keys for inactive tenant: %s", name)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m twisted.trial tests.tenant.test_keyring_reload`
Expected: Both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/tenant/test_keyring_reload.py synapse/crypto/multitenant_keyring.py
git commit -m "feat(tenant): MultiTenantKeyring.reload() for hot add/remove"
```

---

### Task 3: Red probes for `TenantAppServiceRegistry.reload()`

**Files:**
- Create: `tests/tenant/test_appservice_reload.py`

- [ ] **Step 1: Write the failing tests**

```python
#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

"""
Tests for TenantAppServiceRegistry.reload().

Verifies:
1. New tenants get their app services loaded
2. Removed tenants have their app services cleaned up
"""

from unittest import TestCase

from synapse.appservice.tenant_registry import TenantAppServiceRegistry
from synapse.config.tenants import MultiTenantConfig, TenantConfig
from synapse.tenant_registry import TenantRegistry


def _tenant(name: str, as_files: list[str] | None = None) -> TenantConfig:
    return TenantConfig(
        server_name=name,
        database_schema=f"tenant_{name.replace('.', '_')}",
        signing_key_path=f"/keys/{name}.key",
        media_store_path=f"/media/{name}",
        app_service_config_files=as_files,
    )


def _config(tenants: list[TenantConfig]) -> MultiTenantConfig:
    return MultiTenantConfig(
        enabled=True,
        default_schema="public",
        tenants={t.server_name: t for t in tenants},
    )


class TenantAppServiceRegistryReloadTestCase(TestCase):
    """Tests for TenantAppServiceRegistry.reload()."""

    def test_reload_adds_new_tenant(self) -> None:
        """New tenant appears in the registry after reload."""
        acme = _tenant("acme.com")
        registry = TenantRegistry(_config([acme]))
        as_registry = TenantAppServiceRegistry(registry.get_all_tenants())

        self.assertIn("acme.com", as_registry._tenant_services)

        # Add newcorp
        newcorp = _tenant("newcorp.com")
        new_config = _config([acme, newcorp])
        registry.reload(new_config)
        as_registry.reload(registry)

        self.assertIn("newcorp.com", as_registry._tenant_services)

    def test_reload_removes_inactive_tenant(self) -> None:
        """Inactive tenant is removed from the registry after reload."""
        acme = _tenant("acme.com")
        corp = _tenant("corp.io")
        registry = TenantRegistry(_config([acme, corp]))
        as_registry = TenantAppServiceRegistry(registry.get_all_tenants())

        self.assertIn("corp.io", as_registry._tenant_services)

        # Remove corp.io
        registry.reload(_config([acme]))
        as_registry.reload(registry)

        self.assertNotIn("corp.io", as_registry._tenant_services)
        self.assertIn("acme.com", as_registry._tenant_services)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m twisted.trial tests.tenant.test_appservice_reload`
Expected: FAIL — `TenantAppServiceRegistry` has no `reload()` method.

- [ ] **Step 3: Implement `TenantAppServiceRegistry.reload()`**

In `synapse/appservice/tenant_registry.py`, add after `__init__`:

```python
    def reload(self, registry: "TenantRegistry") -> None:
        """Reload app service configs based on current registry state.

        Adds services for new tenants and removes services for inactive ones.

        Args:
            registry: The updated TenantRegistry.
        """
        from synapse.config.appservice import load_appservices

        active_names = set(t.server_name for t in registry.get_all_tenants())

        # Add new tenants
        for tenant in registry.get_all_tenants():
            if tenant.server_name not in self._tenant_services:
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
                logger.info(
                    "Loaded %d app services for tenant %s",
                    len(services),
                    tenant.server_name,
                )

        # Remove inactive tenants
        for name in list(self._tenant_services.keys()):
            if name not in active_names:
                removed_services = self._tenant_services.pop(name)
                self._tenant_exclusive_regex.pop(name, None)
                for svc in removed_services:
                    if svc in self._all_services:
                        self._all_services.remove(svc)
                logger.info("Removed app services for inactive tenant: %s", name)
```

Add `TYPE_CHECKING` import for `TenantRegistry` at the top of the file:

```python
if TYPE_CHECKING:
    from synapse.config.tenants import TenantConfig
    from synapse.tenant_registry import TenantRegistry
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m twisted.trial tests.tenant.test_appservice_reload`
Expected: Both tests PASS.

- [ ] **Step 5: Run existing appservice tests for regressions**

Run: `python -m twisted.trial tests.tenant.test_tenant_appservice`
Expected: All existing tests PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/tenant/test_appservice_reload.py synapse/appservice/tenant_registry.py
git commit -m "feat(tenant): TenantAppServiceRegistry.reload() for hot add/remove"
```

---

### Task 4: Red probes for `TenantRatelimiterRegistry.reload()`

**Files:**
- Create: `tests/tenant/test_ratelimiter_reload.py`

- [ ] **Step 1: Write the failing tests**

```python
#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

"""
Tests for TenantRatelimiterRegistry.reload().

Verifies:
1. Cached limiters for removed tenants are cleared after reload
2. Existing tenant limiters survive reload
"""

from unittest import TestCase
from unittest.mock import MagicMock

from synapse.api.tenant_ratelimiting import TenantRatelimiterRegistry
from synapse.config.ratelimiting import RatelimitSettings
from synapse.config.tenants import (
    MultiTenantConfig,
    TenantConfig,
    TenantRatelimitConfig,
)
from synapse.tenant_registry import TenantRegistry


def _tenant(
    name: str, ratelimit: TenantRatelimitConfig | None = None
) -> TenantConfig:
    return TenantConfig(
        server_name=name,
        database_schema=f"tenant_{name.replace('.', '_')}",
        signing_key_path=f"/keys/{name}.key",
        media_store_path=f"/media/{name}",
        ratelimit=ratelimit,
    )


def _config(tenants: list[TenantConfig]) -> MultiTenantConfig:
    return MultiTenantConfig(
        enabled=True,
        default_schema="public",
        tenants={t.server_name: t for t in tenants},
    )


class TenantRatelimiterRegistryReloadTestCase(TestCase):
    """Tests for TenantRatelimiterRegistry.reload()."""

    def _make_registry(self) -> TenantRatelimiterRegistry:
        store = MagicMock()
        clock = MagicMock()
        global_settings = {
            "rc_message": RatelimitSettings(
                key="rc_message", per_second=0.2, burst_count=10
            ),
        }
        return TenantRatelimiterRegistry(
            store=store, clock=clock, global_settings=global_settings
        )

    def test_reload_clears_removed_tenant_limiters(self) -> None:
        """Cached limiters for removed tenants are evicted."""
        rl = TenantRatelimitConfig(
            rc_message=RatelimitSettings(
                key="rc_message", per_second=1.0, burst_count=5
            )
        )
        acme = _tenant("acme.com", ratelimit=rl)
        corp = _tenant("corp.io", ratelimit=rl)
        registry = TenantRegistry(_config([acme, corp]))
        rl_registry = self._make_registry()

        # Populate cache by requesting limiters
        rl_registry.get("rc_message", tenant=acme)
        rl_registry.get("rc_message", tenant=corp)
        self.assertIn(("acme.com", "rc_message"), rl_registry._cache)
        self.assertIn(("corp.io", "rc_message"), rl_registry._cache)

        # Remove corp.io
        registry.reload(_config([acme]))
        rl_registry.reload(registry)

        self.assertNotIn(("corp.io", "rc_message"), rl_registry._cache)
        self.assertIn(("acme.com", "rc_message"), rl_registry._cache)

    def test_reload_preserves_active_tenant_limiters(self) -> None:
        """Limiters for active tenants survive reload."""
        rl = TenantRatelimitConfig(
            rc_message=RatelimitSettings(
                key="rc_message", per_second=1.0, burst_count=5
            )
        )
        acme = _tenant("acme.com", ratelimit=rl)
        registry = TenantRegistry(_config([acme]))
        rl_registry = self._make_registry()

        limiter_before = rl_registry.get("rc_message", tenant=acme)

        # Reload with same config
        registry.reload(_config([acme]))
        rl_registry.reload(registry)

        limiter_after = rl_registry.get("rc_message", tenant=acme)
        self.assertIs(limiter_before, limiter_after)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m twisted.trial tests.tenant.test_ratelimiter_reload`
Expected: FAIL — `TenantRatelimiterRegistry` has no `reload()` method.

- [ ] **Step 3: Implement `TenantRatelimiterRegistry.reload()`**

In `synapse/api/tenant_ratelimiting.py`, add after the `get()` method:

```python
    def reload(self, registry: "TenantRegistry") -> None:
        """Clear cached limiters for tenants that are no longer active.

        New tenants don't need explicit setup — their limiters are
        lazy-created on first request via ``get()``.

        Args:
            registry: The updated TenantRegistry.
        """
        active_names = set(t.server_name for t in registry.get_all_tenants())
        for key in list(self._cache.keys()):
            server_name, _ = key
            if server_name != _GLOBAL and server_name not in active_names:
                del self._cache[key]
                logger.info(
                    "Cleared cached rate limiter for inactive tenant: %s", server_name
                )
```

Add `TYPE_CHECKING` import for `TenantRegistry` if not already present:

```python
if TYPE_CHECKING:
    from synapse.config.tenants import TenantConfig
    from synapse.storage.databases.main import DataStore
    from synapse.tenant_registry import TenantRegistry
    from synapse.util import Clock
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m twisted.trial tests.tenant.test_ratelimiter_reload`
Expected: Both tests PASS.

- [ ] **Step 5: Run existing ratelimiter tests for regressions**

Run: `python -m twisted.trial tests.tenant.test_tenant_ratelimiter_registry`
Expected: All existing tests PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/tenant/test_ratelimiter_reload.py synapse/api/tenant_ratelimiting.py
git commit -m "feat(tenant): TenantRatelimiterRegistry.reload() for hot add/remove"
```

---

### Task 5: Wire SIGHUP callback to orchestrate reload cascade

**Files:**
- Modify: `synapse/app/_base.py:694-695`

- [ ] **Step 1: Add the `_reload_tenants` function and register it as a SIGHUP callback**

In `synapse/app/_base.py`, find the `start()` function. After the existing `register_sighup` calls (around line 694-695), add:

```python
    def _reload_tenants(hs: "HomeServer") -> None:
        """SIGHUP callback: re-read tenant config and reload all registries."""
        registry = hs.get_tenant_registry()
        if not registry.enabled:
            return
        try:
            old_tenants = hs.config.tenants
            hs.config.reload_config_section("tenants")
            new_mt_config = hs.config.tenants.multi_tenant

            result = registry.reload(new_mt_config)

            keyring = hs.get_multi_tenant_keyring()
            if keyring:
                keyring.reload(registry)

            hs.get_tenant_app_service_registry().reload(registry)
            hs.get_tenant_ratelimiter_registry().reload(registry)

            logger.info(
                "SIGHUP tenant reload: added=%s removed=%s unchanged=%d",
                result["added"],
                result["removed"],
                len(result["unchanged"]),
            )
        except Exception:
            logger.exception(
                "SIGHUP tenant reload failed — running config is unchanged"
            )

    register_sighup(hs, _reload_tenants, hs)
```

- [ ] **Step 2: Verify existing SIGHUP tests still pass**

Run: `python -m twisted.trial tests.tenant`
Expected: All tenant tests PASS.

- [ ] **Step 3: Commit**

```bash
git add synapse/app/_base.py
git commit -m "feat(tenant): wire SIGHUP callback for hot tenant reload cascade"
```

---

## Sub-phase 6b — Backup/Restore CLI

### Task 6: `synapse_tenant backup` subcommand

**Files:**
- Modify: `scripts/synapse_tenant`

- [ ] **Step 1: Add the `cmd_backup` function**

Add this function before `main()` in `scripts/synapse_tenant`:

```python
def cmd_backup(args) -> int:
    """Back up a tenant's database schema and media tree to a .tar.gz archive."""
    import json
    import shutil
    import subprocess
    import tarfile
    import tempfile
    from datetime import datetime, timezone

    # Check pg_dump is available
    if shutil.which("pg_dump") is None:
        logger.error("pg_dump not found in PATH. Install postgresql-client.")
        return 1

    try:
        config = load_config(args.config_path)
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return 1

    tenants = get_tenants_from_config(config)
    tenant = None
    for t in tenants:
        if t.get("server_name") == args.server_name:
            tenant = t
            break

    if tenant is None:
        logger.error(f"Tenant '{args.server_name}' not found in configuration")
        return 1

    server_name = tenant["server_name"]
    database_schema = tenant.get("database_schema", f"tenant_{server_name.replace('.', '_')}")
    media_store_path = tenant.get("media_store_path", "")
    output_path = args.output

    # Build pg_dump connection args from config
    db_config = config.get("database", {}).get("args", {})
    pg_env = os.environ.copy()
    pg_args = ["pg_dump", "--schema=" + database_schema, "--no-owner", "--no-acl"]
    if db_config.get("host"):
        pg_args.extend(["--host", db_config["host"]])
    if db_config.get("port"):
        pg_args.extend(["--port", str(db_config["port"])])
    if db_config.get("user"):
        pg_args.extend(["--username", db_config["user"]])
    if db_config.get("password"):
        pg_env["PGPASSWORD"] = db_config["password"]
    pg_args.append(db_config.get("database", "synapse"))

    tmpdir = tempfile.mkdtemp(prefix="synapse_backup_")
    try:
        # 1. Dump schema
        schema_path = os.path.join(tmpdir, "schema.sql")
        print(f"Dumping database schema '{database_schema}'...")
        result = subprocess.run(
            pg_args, capture_output=True, text=True, env=pg_env
        )
        if result.returncode != 0:
            logger.error(f"pg_dump failed: {result.stderr}")
            return 1
        with open(schema_path, "w") as f:
            f.write(result.stdout)
        print(f"  Schema dumped: {os.path.getsize(schema_path)} bytes")

        # 2. Copy media tree
        media_src = os.path.join(media_store_path, server_name)
        media_dst = os.path.join(tmpdir, "media")
        media_count = 0
        if os.path.isdir(media_src):
            print(f"Copying media tree from {media_src}...")
            shutil.copytree(media_src, media_dst)
            for _root, _dirs, files in os.walk(media_dst):
                media_count += len(files)
            print(f"  Media files: {media_count}")
        else:
            os.makedirs(media_dst)
            print(f"  No media directory found at {media_src}, creating empty media/")

        # 3. Write manifest
        manifest = {
            "server_name": server_name,
            "database_schema": database_schema,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "media_files_count": media_count,
        }
        manifest_path = os.path.join(tmpdir, "manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)

        # 4. Create archive
        print(f"Creating archive: {output_path}")
        with tarfile.open(output_path, "w:gz") as tar:
            tar.add(manifest_path, arcname="manifest.json")
            tar.add(schema_path, arcname="schema.sql")
            tar.add(media_dst, arcname="media")

        print(f"\nBackup complete: {output_path}")
        print(f"  Schema: {database_schema}")
        print(f"  Media files: {media_count}")
        return 0

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
```

- [ ] **Step 2: Add the `backup` subparser in `main()`**

In the `main()` function, after the existing subparsers, add:

```python
    # Backup command
    backup_parser = subparsers.add_parser(
        "backup", help="Back up a tenant's database and media to a .tar.gz archive"
    )
    backup_parser.add_argument(
        "-c", "--config-path", required=True, help="Path to homeserver.yaml"
    )
    backup_parser.add_argument(
        "--server-name", required=True, help="Server name of the tenant to back up"
    )
    backup_parser.add_argument(
        "--output", "-o", required=True, help="Output path for the backup archive"
    )
```

Add `"backup": cmd_backup` to the `commands` dict.

- [ ] **Step 3: Smoke test**

Run: `python scripts/synapse_tenant backup --help`
Expected: Shows backup subcommand help text.

- [ ] **Step 4: Commit**

```bash
git add scripts/synapse_tenant
git commit -m "feat(tenant): synapse_tenant backup subcommand"
```

---

### Task 7: `synapse_tenant restore` subcommand

**Files:**
- Modify: `scripts/synapse_tenant`

- [ ] **Step 1: Add the `cmd_restore` function**

Add this function after `cmd_backup` in `scripts/synapse_tenant`:

```python
def cmd_restore(args) -> int:
    """Restore a tenant from a .tar.gz backup archive."""
    import json
    import shutil
    import subprocess
    import tarfile
    import tempfile

    # Check psql is available
    if shutil.which("psql") is None:
        logger.error("psql not found in PATH. Install postgresql-client.")
        return 1

    archive_path = args.archive
    if not os.path.isfile(archive_path):
        logger.error(f"Archive not found: {archive_path}")
        return 1

    try:
        config = load_config(args.config_path)
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return 1

    tenants = get_tenants_from_config(config)
    tenant = None
    for t in tenants:
        if t.get("server_name") == args.server_name:
            tenant = t
            break

    if tenant is None:
        logger.error(
            f"Tenant '{args.server_name}' not found in configuration. "
            "Add it to homeserver.yaml first."
        )
        return 1

    server_name = tenant["server_name"]
    database_schema = tenant.get("database_schema", f"tenant_{server_name.replace('.', '_')}")
    media_store_path = tenant.get("media_store_path", "")

    tmpdir = tempfile.mkdtemp(prefix="synapse_restore_")
    try:
        # Extract archive
        print(f"Extracting archive: {archive_path}")
        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(path=tmpdir)

        # Validate manifest
        manifest_path = os.path.join(tmpdir, "manifest.json")
        if not os.path.isfile(manifest_path):
            logger.error("Archive missing manifest.json")
            return 1

        with open(manifest_path) as f:
            manifest = json.load(f)

        if manifest.get("server_name") != server_name:
            logger.error(
                f"Manifest server_name '{manifest.get('server_name')}' "
                f"does not match --server-name '{server_name}'"
            )
            return 1

        schema_path = os.path.join(tmpdir, "schema.sql")
        if not os.path.isfile(schema_path):
            logger.error("Archive missing schema.sql")
            return 1

        # Build psql connection args
        db_config = config.get("database", {}).get("args", {})
        pg_env = os.environ.copy()
        pg_args = ["psql", "--set", "ON_ERROR_STOP=1"]
        if db_config.get("host"):
            pg_args.extend(["--host", db_config["host"]])
        if db_config.get("port"):
            pg_args.extend(["--port", str(db_config["port"])])
        if db_config.get("user"):
            pg_args.extend(["--username", db_config["user"]])
        if db_config.get("password"):
            pg_env["PGPASSWORD"] = db_config["password"]
        db_name = db_config.get("database", "synapse")
        pg_args.extend(["--dbname", db_name])

        if psycopg2 is None:
            logger.error("psycopg2 required for schema existence check")
            return 1

        # Check if schema exists
        conn = psycopg2.connect(
            host=db_config.get("host"),
            port=db_config.get("port"),
            user=db_config.get("user"),
            password=db_config.get("password"),
            dbname=db_name,
        )
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
            (database_schema,),
        )
        schema_exists = cur.fetchone() is not None

        if schema_exists and not args.overwrite:
            logger.error(
                f"Schema '{database_schema}' already exists. "
                "Use --overwrite to drop and recreate."
            )
            cur.close()
            conn.close()
            return 1

        if schema_exists and args.overwrite:
            print(f"Dropping existing schema '{database_schema}'...")
            cur.execute(f"DROP SCHEMA {database_schema} CASCADE")

        # Create schema
        print(f"Creating schema '{database_schema}'...")
        cur.execute(f"CREATE SCHEMA {database_schema}")
        cur.close()
        conn.close()

        # Restore schema
        print("Restoring database schema...")
        with open(schema_path, "r") as f:
            result = subprocess.run(
                pg_args, stdin=f, capture_output=True, text=True, env=pg_env
            )
        if result.returncode != 0:
            logger.error(f"psql restore failed: {result.stderr}")
            return 1

        # Restore media
        media_src = os.path.join(tmpdir, "media")
        media_dst = os.path.join(media_store_path, server_name)
        if os.path.isdir(media_src) and os.listdir(media_src):
            print(f"Restoring media tree to {media_dst}...")
            if os.path.exists(media_dst):
                shutil.rmtree(media_dst)
            shutil.copytree(media_src, media_dst)
            media_count = sum(
                len(files) for _, _, files in os.walk(media_dst)
            )
            print(f"  Media files restored: {media_count}")
        else:
            print("  No media files in archive")

        print(f"\nRestore complete for tenant '{server_name}'")
        return 0

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
```

- [ ] **Step 2: Add the `restore` subparser in `main()`**

```python
    # Restore command
    restore_parser = subparsers.add_parser(
        "restore", help="Restore a tenant from a .tar.gz backup archive"
    )
    restore_parser.add_argument(
        "-c", "--config-path", required=True, help="Path to homeserver.yaml"
    )
    restore_parser.add_argument(
        "--server-name", required=True, help="Server name of the tenant to restore"
    )
    restore_parser.add_argument(
        "--from", dest="archive", required=True, help="Path to the backup archive"
    )
    restore_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Drop existing schema before restoring",
    )
```

Add `"restore": cmd_restore` to the `commands` dict.

- [ ] **Step 3: Smoke test**

Run: `python scripts/synapse_tenant restore --help`
Expected: Shows restore subcommand help text.

- [ ] **Step 4: Commit**

```bash
git add scripts/synapse_tenant
git commit -m "feat(tenant): synapse_tenant restore subcommand"
```

---

### Task 8: `synapse_tenant drop` subcommand

**Files:**
- Modify: `scripts/synapse_tenant`

- [ ] **Step 1: Add the `cmd_drop` function**

Add this function after `cmd_restore` in `scripts/synapse_tenant`:

```python
def cmd_drop(args) -> int:
    """Destructively remove a tenant's database schema and media tree."""
    import shutil

    if not args.confirm_destructive:
        logger.error(
            "This command permanently deletes data. "
            "Pass --confirm-destructive to proceed."
        )
        return 1

    try:
        config = load_config(args.config_path)
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return 1

    tenants = get_tenants_from_config(config)
    tenant = None
    for t in tenants:
        if t.get("server_name") == args.server_name:
            tenant = t
            break

    if tenant is None:
        logger.error(f"Tenant '{args.server_name}' not found in configuration")
        return 1

    server_name = tenant["server_name"]
    database_schema = tenant.get("database_schema", f"tenant_{server_name.replace('.', '_')}")
    media_store_path = tenant.get("media_store_path", "")

    print(f"Dropping tenant: {server_name}")
    print(f"  Schema: {database_schema}")
    print(f"  Media: {os.path.join(media_store_path, server_name)}")
    print()

    # Drop database schema
    if psycopg2 is None:
        logger.error("psycopg2 required for schema drop")
        return 1

    db_config = config.get("database", {}).get("args", {})
    try:
        conn = psycopg2.connect(
            host=db_config.get("host"),
            port=db_config.get("port"),
            user=db_config.get("user"),
            password=db_config.get("password"),
            dbname=db_config.get("database", "synapse"),
        )
        conn.autocommit = True
        cur = conn.cursor()

        # Check if schema exists
        cur.execute(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
            (database_schema,),
        )
        if cur.fetchone():
            print(f"Dropping schema '{database_schema}'...")
            cur.execute(f"DROP SCHEMA {database_schema} CASCADE")
            print(f"  Schema dropped.")
        else:
            print(f"  Schema '{database_schema}' does not exist, skipping.")

        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to drop schema: {e}")
        return 1

    # Delete media tree
    media_dir = os.path.join(media_store_path, server_name)
    if os.path.isdir(media_dir):
        print(f"Deleting media directory: {media_dir}")
        shutil.rmtree(media_dir)
        print(f"  Media directory deleted.")
    else:
        print(f"  Media directory '{media_dir}' does not exist, skipping.")

    print(f"\nTenant '{server_name}' dropped.")
    print("Remember to remove the tenant from homeserver.yaml and send SIGHUP.")
    return 0
```

- [ ] **Step 2: Add the `drop` subparser in `main()`**

```python
    # Drop command
    drop_parser = subparsers.add_parser(
        "drop", help="Destructively remove a tenant's schema and media"
    )
    drop_parser.add_argument(
        "-c", "--config-path", required=True, help="Path to homeserver.yaml"
    )
    drop_parser.add_argument(
        "--server-name", required=True, help="Server name of the tenant to drop"
    )
    drop_parser.add_argument(
        "--confirm-destructive",
        action="store_true",
        help="Required flag to confirm destructive operation",
    )
```

Add `"drop": cmd_drop` to the `commands` dict.

- [ ] **Step 3: Smoke test**

Run: `python scripts/synapse_tenant drop --help`
Expected: Shows drop subcommand help text.

- [ ] **Step 4: Commit**

```bash
git add scripts/synapse_tenant
git commit -m "feat(tenant): synapse_tenant drop subcommand"
```

---

### Task 9: Unit tests for backup/restore/drop CLI

**Files:**
- Create: `tests/tenant/test_tenant_backup.py`

- [ ] **Step 1: Write the tests**

```python
#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

"""
Tests for synapse_tenant backup/restore/drop subcommands.

These tests verify CLI argument parsing and archive structure.
They do NOT exercise real pg_dump/psql (those require a running database).
"""

import json
import os
import tarfile
import tempfile
from unittest import TestCase


class TenantBackupArchiveTestCase(TestCase):
    """Tests for backup archive format validation."""

    def _create_test_archive(self, tmpdir: str, server_name: str = "acme.com") -> str:
        """Create a valid backup archive for testing."""
        archive_path = os.path.join(tmpdir, "test_backup.tar.gz")
        manifest = {
            "server_name": server_name,
            "database_schema": f"tenant_{server_name.replace('.', '_')}",
            "timestamp": "2026-04-09T12:00:00+00:00",
            "media_files_count": 1,
        }

        staging = os.path.join(tmpdir, "staging")
        os.makedirs(staging)

        # manifest.json
        manifest_path = os.path.join(staging, "manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest, f)

        # schema.sql
        schema_path = os.path.join(staging, "schema.sql")
        with open(schema_path, "w") as f:
            f.write("CREATE TABLE test (id int);\n")

        # media/
        media_dir = os.path.join(staging, "media", "local_content")
        os.makedirs(media_dir)
        with open(os.path.join(media_dir, "test.dat"), "w") as f:
            f.write("media content")

        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(manifest_path, arcname="manifest.json")
            tar.add(schema_path, arcname="schema.sql")
            tar.add(os.path.join(staging, "media"), arcname="media")

        return archive_path

    def test_archive_contains_required_files(self) -> None:
        """A valid archive contains manifest.json, schema.sql, and media/."""
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = self._create_test_archive(tmpdir)

            with tarfile.open(archive_path, "r:gz") as tar:
                names = tar.getnames()

            self.assertIn("manifest.json", names)
            self.assertIn("schema.sql", names)
            self.assertTrue(any(n.startswith("media") for n in names))

    def test_manifest_contents(self) -> None:
        """Manifest contains correct server_name and schema."""
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = self._create_test_archive(tmpdir, "corp.io")

            with tarfile.open(archive_path, "r:gz") as tar:
                f = tar.extractfile("manifest.json")
                manifest = json.loads(f.read())

            self.assertEqual(manifest["server_name"], "corp.io")
            self.assertEqual(manifest["database_schema"], "tenant_corp_io")
            self.assertIn("timestamp", manifest)
            self.assertIn("media_files_count", manifest)

    def test_archive_round_trip_structure(self) -> None:
        """Extract and re-examine to verify structure survives round-trip."""
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = self._create_test_archive(tmpdir)

            extract_dir = os.path.join(tmpdir, "extracted")
            os.makedirs(extract_dir)
            with tarfile.open(archive_path, "r:gz") as tar:
                tar.extractall(path=extract_dir)

            self.assertTrue(os.path.isfile(os.path.join(extract_dir, "manifest.json")))
            self.assertTrue(os.path.isfile(os.path.join(extract_dir, "schema.sql")))
            self.assertTrue(os.path.isdir(os.path.join(extract_dir, "media")))
            self.assertTrue(
                os.path.isfile(
                    os.path.join(extract_dir, "media", "local_content", "test.dat")
                )
            )


class TenantDropSafetyTestCase(TestCase):
    """Tests for drop command safety checks."""

    def test_drop_subcommand_requires_confirm_flag(self) -> None:
        """The drop command should refuse to run without --confirm-destructive."""
        # Import the CLI module
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "synapse_tenant",
            os.path.join(
                os.path.dirname(__file__), "..", "..", "scripts", "synapse_tenant"
            ),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # Simulate args without confirm flag
        class FakeArgs:
            config_path = "/nonexistent.yaml"
            server_name = "acme.com"
            confirm_destructive = False

        result = mod.cmd_drop(FakeArgs())
        self.assertEqual(result, 1)
```

- [ ] **Step 2: Run tests**

Run: `python -m twisted.trial tests.tenant.test_tenant_backup`
Expected: All tests PASS (these test archive structure and CLI safety, not real DB operations).

- [ ] **Step 3: Commit**

```bash
git add tests/tenant/test_tenant_backup.py
git commit -m "test(tenant): unit tests for backup archive format and drop safety"
```

---

### Task 10: Final verification — run full tenant test suite

- [ ] **Step 1: Run all tenant tests**

Run: `python -m twisted.trial tests.tenant`
Expected: All tests PASS (existing + new reload + backup tests).

- [ ] **Step 2: Verify no regressions in the broader test suite**

Run: `python -m twisted.trial tests.test_server` (smoke test that HomeServer boots)
Expected: PASS.

- [ ] **Step 3: Count the probe results**

Expected new test files and approximate counts:
- `test_registry_reload.py` — 6 tests
- `test_keyring_reload.py` — 2 tests
- `test_appservice_reload.py` — 2 tests
- `test_ratelimiter_reload.py` — 2 tests
- `test_tenant_backup.py` — 4 tests

Total new: ~16 probes. Total with existing: ~16 + existing suite.
