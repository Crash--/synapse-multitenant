# Phase 10 — Federation Inbound Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make inbound federation tenant-aware so every tenant can receive events, EDUs, and key queries from the public Matrix network.

**Architecture:** Three-tier destination resolution in `Authenticator` (X-Matrix destination → current tenant → global default). Per-tenant key server via `MultiTenantKeyring`. `_effective_server_name` / `_effective_signing_key` property pattern on `FederationServer` and `EventAuthHandler`. `TenantFederationConfig` dataclass for per-tenant allow-lists.

**Tech Stack:** Python/Twisted, Synapse federation transport, MultiTenantKeyring, contextvars for tenant context.

**Test runner:** `trial tests.tenant.test_federation_inbound` (Twisted Trial, not pytest).

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `tests/tenant/test_federation_inbound.py` | Create | All red/green probes for phase 10 (sub-phases 10a-10e) |
| `synapse/federation/transport/server/_base.py` | Modify | Authenticator federation tenant resolution + whitelist |
| `synapse/federation/transport/server/__init__.py` | Modify | Pass tenant_registry to Authenticator |
| `synapse/rest/key/v2/local_key_resource.py` | Modify | Per-tenant key server responses |
| `synapse/rest/key/v2/__init__.py` | Modify | Pass keyring to LocalKey |
| `synapse/federation/federation_server.py` | Modify | `_effective_server_name` / `_effective_signing_key` properties |
| `synapse/config/tenants.py` | Modify | Add `TenantFederationConfig` dataclass |
| `synapse/config/federation.py` | Modify | Tenant-aware `is_domain_allowed` |
| `synapse/handlers/event_auth.py` | Modify | `_effective_server_name` property |

---

## Task 1: Red probes for federation tenant resolution (sub-phase 10a)

**Files:**
- Create: `tests/tenant/test_federation_inbound.py`

- [ ] **Step 1: Write the failing test file with 10a probes**

```python
#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

"""
Red/green probes for phase 10 — federation inbound multi-tenant support.

Probes verify that:
1. Authenticator resolves destination from X-Matrix header and sets tenant context
2. Authenticator falls back to current tenant context when destination is absent
3. Authenticator rejects destination not matching any tenant
4. json_request["destination"] matches what the remote signed
"""

from http import HTTPStatus
from unittest import TestCase
from unittest.mock import AsyncMock, MagicMock, patch

from synapse.api.errors import AuthenticationError
from synapse.config.tenants import TenantConfig
from synapse.federation.transport.server._base import Authenticator


def _make_tenant(name: str) -> TenantConfig:
    return TenantConfig(
        server_name=f"{name}.localhost",
        database_schema=f"tenant_{name}",
        signing_key_path=f"/keys/{name}.key",
        media_store_path=f"/media/{name}",
    )


def _make_hs(
    hostname: str = "main.localhost",
    tenants: list[TenantConfig] | None = None,
) -> MagicMock:
    hs = MagicMock()
    hs.hostname = hostname
    hs.get_clock.return_value = MagicMock(time_msec=MagicMock(return_value=1000))
    hs.get_keyring.return_value = AsyncMock()
    hs.get_datastores.return_value.main = MagicMock()
    hs.config.federation.federation_domain_whitelist = None
    hs.get_notifier.return_value = MagicMock()
    hs.config.worker.worker_app = None

    # Build a tenant registry mock
    registry = MagicMock()
    tenants = tenants or []
    tenant_map = {t.server_name: t for t in tenants}
    registry.get_tenant = MagicMock(side_effect=lambda sn: tenant_map.get(sn))
    hs.get_tenant_registry.return_value = registry

    # is_mine_server_name checks global + all tenants
    all_names = {hostname} | set(tenant_map.keys())
    hs.is_mine_server_name = MagicMock(side_effect=lambda sn: sn in all_names)

    return hs


# ── 10a probes: federation tenant resolution ─────────────────────


class TestAuthenticatorDestinationResolution(TestCase):
    """Authenticator must use X-Matrix destination to set json_request['destination']
    and resolve tenant context."""

    def test_authenticator_accepts_tenant_registry(self) -> None:
        """Authenticator.__init__ must accept and store a tenant_registry parameter."""
        acme = _make_tenant("acme")
        hs = _make_hs(tenants=[acme])
        registry = hs.get_tenant_registry()
        auth = Authenticator(hs, tenant_registry=registry)
        self.assertIs(auth._tenant_registry, registry)

    def test_authenticator_resolve_destination_from_xmatrix(self) -> None:
        """When X-Matrix header has destination=acme.localhost,
        _resolve_federation_destination returns acme.localhost."""
        acme = _make_tenant("acme")
        hs = _make_hs(tenants=[acme])
        registry = hs.get_tenant_registry()
        auth = Authenticator(hs, tenant_registry=registry)
        result = auth._resolve_federation_destination(
            parsed_destination="acme.localhost"
        )
        self.assertEqual(result, "acme.localhost")

    def test_authenticator_resolve_destination_fallback_to_tenant_ctx(self) -> None:
        """When X-Matrix destination is None, falls back to get_current_tenant()."""
        acme = _make_tenant("acme")
        hs = _make_hs(tenants=[acme])
        registry = hs.get_tenant_registry()
        auth = Authenticator(hs, tenant_registry=registry)
        with patch(
            "synapse.federation.transport.server._base.get_current_tenant",
            return_value=acme,
        ):
            result = auth._resolve_federation_destination(parsed_destination=None)
        self.assertEqual(result, "acme.localhost")

    def test_authenticator_resolve_destination_fallback_to_global(self) -> None:
        """When both X-Matrix destination and tenant context are None,
        falls back to self.server_name."""
        hs = _make_hs()
        auth = Authenticator(hs, tenant_registry=MagicMock(get_tenant=MagicMock(return_value=None)))
        with patch(
            "synapse.federation.transport.server._base.get_current_tenant",
            return_value=None,
        ):
            result = auth._resolve_federation_destination(parsed_destination=None)
        self.assertEqual(result, "main.localhost")

    def test_authenticator_rejects_unknown_destination(self) -> None:
        """When X-Matrix destination doesn't match any tenant or global hostname,
        raise AuthenticationError."""
        hs = _make_hs()
        auth = Authenticator(hs, tenant_registry=MagicMock(get_tenant=MagicMock(return_value=None)))
        with self.assertRaises(AuthenticationError):
            auth._resolve_federation_destination(
                parsed_destination="unknown.example.com"
            )

    def test_authenticator_sets_tenant_context(self) -> None:
        """After resolving destination to a tenant, Authenticator must call
        set_current_tenant with the resolved tenant."""
        acme = _make_tenant("acme")
        hs = _make_hs(tenants=[acme])
        registry = hs.get_tenant_registry()
        auth = Authenticator(hs, tenant_registry=registry)
        with patch(
            "synapse.federation.transport.server._base.set_current_tenant"
        ) as mock_set:
            auth._resolve_federation_destination(
                parsed_destination="acme.localhost"
            )
            mock_set.assert_called_once_with(acme)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `trial tests.tenant.test_federation_inbound.TestAuthenticatorDestinationResolution 2>&1 | tail -20`
Expected: FAIL — `Authenticator.__init__` doesn't accept `tenant_registry`, `_resolve_federation_destination` doesn't exist.

- [ ] **Step 3: Commit red probes**

```bash
git add tests/tenant/test_federation_inbound.py
git commit -m "test(phase-10a): add red probes for federation tenant resolution"
```

---

## Task 2: Implement federation tenant resolution (sub-phase 10a)

**Files:**
- Modify: `synapse/federation/transport/server/_base.py:62-147`
- Modify: `synapse/federation/transport/server/__init__.py:72`

- [ ] **Step 1: Add tenant_registry to Authenticator.__init__ and _resolve_federation_destination method**

In `synapse/federation/transport/server/_base.py`, modify `Authenticator.__init__` (line 63) to accept an optional `tenant_registry` parameter, and add the resolution method:

```python
class Authenticator:
    def __init__(self, hs: "HomeServer", tenant_registry: "TenantRegistry | None" = None):
        self._clock = hs.get_clock()
        self.keyring = hs.get_keyring()
        self.server_name = hs.hostname
        self._is_mine_server_name = hs.is_mine_server_name
        self.store = hs.get_datastores().main
        self.federation_domain_whitelist = (
            hs.config.federation.federation_domain_whitelist
        )
        self.notifier = hs.get_notifier()
        self._hs = hs
        self._tenant_registry = tenant_registry

        self.replication_client = None
        if hs.config.worker.worker_app:
            self.replication_client = hs.get_replication_command_handler()

    def _resolve_federation_destination(
        self, parsed_destination: str | None
    ) -> str:
        """Resolve the effective destination for federation signature verification.

        Three-tier resolution:
        1. X-Matrix destination field (what the remote signed against)
        2. Current tenant context (from Host / X-Matrix-Server-Name)
        3. Global server_name (backwards compat)

        Also sets tenant context when destination resolves to a tenant.

        Args:
            parsed_destination: The destination from the X-Matrix Authorization
                header, or None if not present.

        Returns:
            The resolved destination server_name.

        Raises:
            AuthenticationError: If destination is present but doesn't match
                any tenant or the global hostname.
        """
        if parsed_destination is not None:
            # Tier 1: explicit destination from X-Matrix header
            if not self._is_mine_server_name(parsed_destination):
                raise AuthenticationError(
                    HTTPStatus.UNAUTHORIZED,
                    f"Destination {parsed_destination!r} is not served by this homeserver",
                    Codes.UNAUTHORIZED,
                )
            # Try to set tenant context
            if self._tenant_registry is not None:
                tenant = self._tenant_registry.get_tenant(parsed_destination)
                if tenant is not None:
                    set_current_tenant(tenant)
            return parsed_destination

        # Tier 2: current tenant context (set by _setup_tenant_context)
        current_tenant = get_current_tenant()
        if current_tenant is not None:
            return current_tenant.server_name

        # Tier 3: global default
        return self.server_name
```

Add imports at the top of `_base.py`:

```python
from synapse.tenant_context import get_current_tenant, set_current_tenant
```

And add `TYPE_CHECKING` import for `TenantRegistry`:

```python
if TYPE_CHECKING:
    from synapse.tenant_registry import TenantRegistry
```

- [ ] **Step 2: Modify authenticate_request to use _resolve_federation_destination**

In `authenticate_request` (line 79), after the auth header loop collects `destination` values, replace the hardcoded `json_request["destination"] = self.server_name` with the resolution method. The modified method:

```python
    async def authenticate_request(
        self, request: SynapseRequest, content: JsonDict | None
    ) -> str:
        now = self._clock.time_msec()
        json_request: JsonDict = {
            "method": request.method.decode("ascii"),
            "uri": request.uri.decode("ascii"),
            "destination": self.server_name,  # placeholder, overwritten below
            "signatures": {},
        }

        if content is not None:
            json_request["content"] = content

        origin = None
        last_destination: str | None = None

        auth_headers = request.requestHeaders.getRawHeaders(b"Authorization")

        if not auth_headers:
            raise NoAuthenticationError(
                HTTPStatus.UNAUTHORIZED,
                "Missing Authorization headers",
                Codes.UNAUTHORIZED,
            )

        for auth in auth_headers:
            if auth.startswith(b"X-Matrix"):
                (origin, key, sig, destination) = _parse_auth_header(auth)
                json_request["origin"] = origin
                json_request["signatures"].setdefault(origin, {})[key] = sig

                if destination is not None:
                    last_destination = destination

                # if the origin_server sent a destination along it needs to match our own server_name
                if destination is not None and not self._is_mine_server_name(
                    destination
                ):
                    raise AuthenticationError(
                        HTTPStatus.UNAUTHORIZED,
                        f"Destination mismatch in auth header, received: {destination!r}",
                        Codes.UNAUTHORIZED,
                    )

        # Resolve effective destination and set tenant context
        effective_destination = self._resolve_federation_destination(last_destination)
        json_request["destination"] = effective_destination

        if (
            self.federation_domain_whitelist is not None
            and origin not in self.federation_domain_whitelist
        ):
            raise FederationDeniedError(origin)

        if origin is None or not json_request["signatures"]:
            raise NoAuthenticationError(
                HTTPStatus.UNAUTHORIZED,
                "Missing Authorization headers",
                Codes.UNAUTHORIZED,
            )

        await self.keyring.verify_json_for_server(
            origin,
            json_request,
            now,
        )

        logger.debug("Request from %s", origin)
        request.requester = origin

        # If we get a valid signed request from the other side, its probably
        # alive
        retry_timings = await self.store.get_destination_retry_timings(origin)
        if retry_timings and retry_timings.retry_last_ts:
            run_in_background(self.reset_retry_timings, origin)

        return origin
```

- [ ] **Step 3: Pass tenant_registry when constructing Authenticator**

In `synapse/federation/transport/server/__init__.py` line 72, change:

```python
self.authenticator = Authenticator(hs)
```

to:

```python
self.authenticator = Authenticator(hs, tenant_registry=hs.get_tenant_registry())
```

Verify `get_tenant_registry` exists on HomeServer. If not, use:

```python
registry = getattr(hs, 'get_tenant_registry', lambda: None)()
self.authenticator = Authenticator(hs, tenant_registry=registry)
```

- [ ] **Step 4: Run tests to verify probes go green**

Run: `trial tests.tenant.test_federation_inbound.TestAuthenticatorDestinationResolution 2>&1 | tail -20`
Expected: All 6 tests PASS.

- [ ] **Step 5: Run existing federation tests to verify no regression**

Run: `trial tests.tenant.test_federation_sender_multitenant 2>&1 | tail -5`
Expected: All existing tests PASS.

- [ ] **Step 6: Commit**

```bash
git add synapse/federation/transport/server/_base.py synapse/federation/transport/server/__init__.py
git commit -m "feat(phase-10a): resolve federation destination from X-Matrix header and set tenant context"
```

---

## Task 3: Red probes for per-tenant key server (sub-phase 10b)

**Files:**
- Modify: `tests/tenant/test_federation_inbound.py`

- [ ] **Step 1: Append 10b probes to the test file**

```python
# ── 10b probes: per-tenant key server ────────────────────────────


class TestLocalKeyTenantAware(TestCase):
    """LocalKey.on_GET must return per-tenant keys when tenant context is set."""

    def _make_keyring(self, tenants: list[TenantConfig]) -> MagicMock:
        """Build a mock MultiTenantKeyring with signing + verify keys per tenant."""
        from signedjson import key as key_mod

        keyring = MagicMock()
        signing_keys: dict[str, list] = {}
        verify_keys: dict[str, dict] = {}

        for t in tenants:
            sk = key_mod.generate_signing_key("0")
            signing_keys[t.server_name] = [sk]
            vk = key_mod.get_verify_key(sk)
            key_id = f"{vk.alg}:{vk.version}"
            verify_keys[t.server_name] = {key_id: vk}

        keyring.get_all_signing_keys = MagicMock(
            side_effect=lambda sn: signing_keys[sn]
        )
        keyring.get_verify_keys = MagicMock(
            side_effect=lambda sn: verify_keys[sn]
        )
        return keyring

    def test_local_key_accepts_keyring(self) -> None:
        """LocalKey.__init__ must accept an optional multi_tenant_keyring parameter."""
        from synapse.rest.key.v2.local_key_resource import LocalKey

        hs = MagicMock()
        hs.config.key.signing_key = [MagicMock()]
        hs.config.key.old_signing_keys = {}
        hs.config.key.key_refresh_interval = 86400000
        hs.config.server.server_name = "main.localhost"
        hs.get_clock.return_value = MagicMock(time_msec=MagicMock(return_value=1000))

        keyring = self._make_keyring([_make_tenant("acme")])
        local_key = LocalKey(hs, multi_tenant_keyring=keyring)
        self.assertIs(local_key._multi_tenant_keyring, keyring)

    def test_local_key_returns_tenant_server_name(self) -> None:
        """When tenant context is set, on_GET response must have tenant's server_name."""
        from synapse.rest.key.v2.local_key_resource import LocalKey

        hs = MagicMock()
        hs.config.key.signing_key = [MagicMock()]
        hs.config.key.old_signing_keys = {}
        hs.config.key.key_refresh_interval = 86400000
        hs.config.server.server_name = "main.localhost"
        hs.get_clock.return_value = MagicMock(time_msec=MagicMock(return_value=1000))

        acme = _make_tenant("acme")
        keyring = self._make_keyring([acme])
        local_key = LocalKey(hs, multi_tenant_keyring=keyring)

        with patch(
            "synapse.rest.key.v2.local_key_resource.get_current_tenant",
            return_value=acme,
        ):
            status, body = local_key.on_GET(MagicMock(), key_id=None)

        self.assertEqual(status, 200)
        self.assertEqual(body["server_name"], "acme.localhost")

    def test_local_key_returns_global_when_no_tenant(self) -> None:
        """When no tenant context is set, on_GET returns the global key response."""
        from synapse.rest.key.v2.local_key_resource import LocalKey

        hs = MagicMock()
        hs.config.key.signing_key = [MagicMock()]
        hs.config.key.old_signing_keys = {}
        hs.config.key.key_refresh_interval = 86400000
        hs.config.server.server_name = "main.localhost"
        hs.get_clock.return_value = MagicMock(time_msec=MagicMock(return_value=1000))

        local_key = LocalKey(hs)

        with patch(
            "synapse.rest.key.v2.local_key_resource.get_current_tenant",
            return_value=None,
        ):
            status, body = local_key.on_GET(MagicMock(), key_id=None)

        self.assertEqual(status, 200)
        self.assertEqual(body["server_name"], "main.localhost")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `trial tests.tenant.test_federation_inbound.TestLocalKeyTenantAware 2>&1 | tail -20`
Expected: FAIL — `LocalKey.__init__` doesn't accept `multi_tenant_keyring`.

- [ ] **Step 3: Commit red probes**

```bash
git add tests/tenant/test_federation_inbound.py
git commit -m "test(phase-10b): add red probes for per-tenant key server"
```

---

## Task 4: Implement per-tenant key server (sub-phase 10b)

**Files:**
- Modify: `synapse/rest/key/v2/local_key_resource.py:40-126`
- Modify: `synapse/rest/key/v2/__init__.py:39`

- [ ] **Step 1: Make LocalKey tenant-aware**

Modify `local_key_resource.py`. Add import at top:

```python
from synapse.tenant_context import get_current_tenant
```

And add `TYPE_CHECKING` import:

```python
if TYPE_CHECKING:
    from synapse.crypto.multitenant_keyring import MultiTenantKeyring
```

Replace the `LocalKey` class:

```python
class LocalKey(RestServlet):
    """HTTP resource containing encoding the TLS X.509 certificate and NACL
    signature verification keys for this server::

        GET /_matrix/key/v2/server HTTP/1.1

        GET /_matrix/key/v2/server/a.key.id HTTP/1.1

        HTTP/1.1 200 OK
        Content-Type: application/json
        {
            "valid_until_ts": # integer posix timestamp when this result expires.
            "server_name": "this.server.example.com"
            "verify_keys": {
                "algorithm:version": {
                    "key": # base64 encoded NACL verification key.
                }
            },
            "old_verify_keys": {
                "algorithm:version": {
                    "expired_ts": # integer posix timestamp when the key expired.
                    "key": # base64 encoded NACL verification key.
                }
            },
            "signatures": {
                "this.server.example.com": {
                   "algorithm:version": # NACL signature for this server
                }
            }
        }
    """

    PATTERNS = (re.compile("^/_matrix/key/v2/server(/(?P<key_id>[^/]*))?$"),)

    def __init__(
        self,
        hs: "HomeServer",
        multi_tenant_keyring: "MultiTenantKeyring | None" = None,
    ):
        self.config = hs.config
        self.clock = hs.get_clock()
        self._multi_tenant_keyring = multi_tenant_keyring
        # Global (default) response — cached per existing logic
        self.update_response_body(self.clock.time_msec())
        # Per-tenant cache: server_name -> (valid_until_ts, response_body)
        self._tenant_responses: dict[str, tuple[int, JsonDict]] = {}

    def update_response_body(self, time_now_msec: int) -> None:
        refresh_interval = self.config.key.key_refresh_interval
        self.valid_until_ts = int(time_now_msec + refresh_interval)
        self.response_body = self.response_json_object()

    def response_json_object(self) -> JsonDict:
        verify_keys = {}
        for signing_key in self.config.key.signing_key:
            verify_key_bytes = signing_key.verify_key.encode()
            key_id = "%s:%s" % (signing_key.alg, signing_key.version)
            verify_keys[key_id] = {"key": encode_base64(verify_key_bytes)}

        old_verify_keys = {}
        for key_id, old_signing_key in self.config.key.old_signing_keys.items():
            verify_key_bytes = old_signing_key.encode()
            old_verify_keys[key_id] = {
                "key": encode_base64(verify_key_bytes),
                "expired_ts": old_signing_key.expired,
            }

        json_object = {
            "valid_until_ts": self.valid_until_ts,
            "server_name": self.config.server.server_name,
            "verify_keys": verify_keys,
            "old_verify_keys": old_verify_keys,
        }
        for key in self.config.key.signing_key:
            json_object = sign_json(json_object, self.config.server.server_name, key)
        return json_object

    def _build_tenant_response(
        self, server_name: str, time_now_msec: int
    ) -> JsonDict:
        """Build a key response for a specific tenant."""
        keyring = self._multi_tenant_keyring
        assert keyring is not None

        signing_keys = keyring.get_all_signing_keys(server_name)

        verify_keys = {}
        for sk in signing_keys:
            vk_bytes = sk.verify_key.encode()
            key_id = "%s:%s" % (sk.alg, sk.version)
            verify_keys[key_id] = {"key": encode_base64(vk_bytes)}

        refresh_interval = self.config.key.key_refresh_interval
        valid_until_ts = int(time_now_msec + refresh_interval)

        json_object: JsonDict = {
            "valid_until_ts": valid_until_ts,
            "server_name": server_name,
            "verify_keys": verify_keys,
            "old_verify_keys": {},
        }
        for sk in signing_keys:
            json_object = sign_json(json_object, server_name, sk)

        self._tenant_responses[server_name] = (valid_until_ts, json_object)
        return json_object

    def on_GET(
        self, request: Request, key_id: str | None = None
    ) -> tuple[int, JsonDict]:
        # Matrix 1.6 drops support for passing the key_id, this is incompatible
        # with earlier versions and is allowed in order to support both.
        if key_id:
            logger.warning(
                "Request for local server key with deprecated key ID (logging to determine usage level for future removal): %s",
                key_id,
            )

        time_now = self.clock.time_msec()

        # Check for tenant context
        tenant = get_current_tenant()
        if tenant is not None and self._multi_tenant_keyring is not None:
            server_name = tenant.server_name
            cached = self._tenant_responses.get(server_name)
            if cached is not None:
                valid_until_ts, body = cached
                if time_now + self.config.key.key_refresh_interval / 2 <= valid_until_ts:
                    return 200, body
            # Cache miss or expired — rebuild
            body = self._build_tenant_response(server_name, time_now)
            return 200, body

        # Global (default) response
        if time_now + self.config.key.key_refresh_interval / 2 > self.valid_until_ts:
            self.update_response_body(time_now)
        return 200, self.response_body
```

- [ ] **Step 2: Pass keyring when constructing LocalKey**

In `synapse/rest/key/v2/__init__.py` line 39, change:

```python
LocalKey(hs).register(http_server)
```

to:

```python
LocalKey(hs, multi_tenant_keyring=hs.get_multi_tenant_keyring()).register(http_server)
```

- [ ] **Step 3: Run tests to verify probes go green**

Run: `trial tests.tenant.test_federation_inbound.TestLocalKeyTenantAware 2>&1 | tail -20`
Expected: All 3 tests PASS.

- [ ] **Step 4: Run existing tests for regression**

Run: `trial tests.tenant.test_federation_sender_multitenant 2>&1 | tail -5`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add synapse/rest/key/v2/local_key_resource.py synapse/rest/key/v2/__init__.py
git commit -m "feat(phase-10b): per-tenant key server via MultiTenantKeyring"
```

---

## Task 5: Red probes for FederationServer tenant-awareness (sub-phase 10c)

**Files:**
- Modify: `tests/tenant/test_federation_inbound.py`

- [ ] **Step 1: Append 10c probes to the test file**

```python
# ── 10c probes: FederationServer tenant-awareness ────────────────


class TestFederationServerEffectiveServerName(TestCase):
    """FederationServer._effective_server_name must resolve from tenant context."""

    def _make_federation_server(
        self, hostname: str = "main.localhost"
    ) -> "FederationServer":
        from synapse.federation.federation_server import FederationServer

        hs = MagicMock()
        hs.hostname = hostname
        hs.get_clock.return_value = MagicMock()
        hs.get_state_handler.return_value = MagicMock()
        hs.get_storage_controllers.return_value = MagicMock()
        hs.get_datastores.return_value = MagicMock()
        hs.get_federation_handler.return_value = MagicMock()
        hs.get_module_api_callbacks.return_value.spam_checker = MagicMock()
        hs.get_federation_event_handler.return_value = MagicMock()
        hs.config.federation.federation_metrics_domains = frozenset()
        hs.signing_key = MagicMock()
        hs.get_multi_tenant_keyring.return_value = MagicMock()
        return FederationServer(hs)

    def test_effective_server_name_with_tenant(self) -> None:
        """_effective_server_name returns tenant's server_name when context is set."""
        fs = self._make_federation_server()
        acme = _make_tenant("acme")
        with patch(
            "synapse.federation.federation_server.get_current_tenant",
            return_value=acme,
        ):
            self.assertEqual(fs._effective_server_name, "acme.localhost")

    def test_effective_server_name_without_tenant(self) -> None:
        """_effective_server_name falls back to self.server_name when no context."""
        fs = self._make_federation_server()
        with patch(
            "synapse.federation.federation_server.get_current_tenant",
            return_value=None,
        ):
            self.assertEqual(fs._effective_server_name, "main.localhost")

    def test_effective_signing_key_with_tenant(self) -> None:
        """_effective_signing_key returns tenant key from MultiTenantKeyring."""
        fs = self._make_federation_server()
        acme = _make_tenant("acme")
        mock_key = MagicMock()
        fs._multi_tenant_keyring = MagicMock()
        fs._multi_tenant_keyring.get_signing_key.return_value = mock_key
        with patch(
            "synapse.federation.federation_server.get_current_tenant",
            return_value=acme,
        ):
            self.assertIs(fs._effective_signing_key, mock_key)
            fs._multi_tenant_keyring.get_signing_key.assert_called_with(
                "acme.localhost"
            )

    def test_effective_signing_key_without_tenant(self) -> None:
        """_effective_signing_key falls back to hs.signing_key when no context."""
        fs = self._make_federation_server()
        mock_key = MagicMock()
        fs.hs.signing_key = mock_key
        with patch(
            "synapse.federation.federation_server.get_current_tenant",
            return_value=None,
        ):
            self.assertIs(fs._effective_signing_key, mock_key)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `trial tests.tenant.test_federation_inbound.TestFederationServerEffectiveServerName 2>&1 | tail -20`
Expected: FAIL — `_effective_server_name` and `_effective_signing_key` don't exist.

- [ ] **Step 3: Commit red probes**

```bash
git add tests/tenant/test_federation_inbound.py
git commit -m "test(phase-10c): add red probes for FederationServer tenant-awareness"
```

---

## Task 6: Implement FederationServer tenant-awareness (sub-phase 10c)

**Files:**
- Modify: `synapse/federation/federation_server.py:136-145, 564-571, 1000-1007, 1138-1144`

- [ ] **Step 1: Add imports and properties to FederationServer**

Add import at top of `federation_server.py`:

```python
from synapse.tenant_context import get_current_tenant
```

In `FederationServer.__init__` (line 137), after `super().__init__(hs)`, add:

```python
        self._multi_tenant_keyring = hs.get_multi_tenant_keyring()
```

After `__init__`, add the two properties:

```python
    @property
    def _effective_server_name(self) -> str:
        """Return the current tenant's server_name, or the global default."""
        tenant = get_current_tenant()
        return tenant.server_name if tenant else self.server_name

    @property
    def _effective_signing_key(self) -> "SigningKey":
        """Return the current tenant's signing key, or the global default."""
        tenant = get_current_tenant()
        if tenant and self._multi_tenant_keyring is not None:
            return self._multi_tenant_keyring.get_signing_key(tenant.server_name)
        return self.hs.signing_key
```

- [ ] **Step 2: Patch EDU handling (line 564-571)**

In `_handle_edus_in_txn`, change line 564:

```python
            received_edus_counter.labels(**{SERVER_NAME_LABEL: self._effective_server_name}).inc()
```

And line 568:

```python
            edu = Edu(
                origin=origin,
                destination=self._effective_server_name,
                edu_type=edu_dict["edu_type"],
                content=edu_dict["content"],
            )
```

- [ ] **Step 3: Patch event signing (line 1000-1007)**

In `on_send_join_request` (around line 1000), change:

```python
            event.signatures.update(
                compute_event_signature(
                    room_version,
                    event.get_pdu_json(),
                    self._effective_server_name,
                    self._effective_signing_key,
                )
            )
```

- [ ] **Step 4: Patch transaction building (line 1138-1144)**

In `_build_get_missing_events_response` (around line 1141), change:

```python
        return Transaction(
            transaction_id="",
            origin=self._effective_server_name,
            pdus=pdus,
            origin_server_ts=int(time_now),
            destination="",
```

- [ ] **Step 5: Run tests to verify probes go green**

Run: `trial tests.tenant.test_federation_inbound.TestFederationServerEffectiveServerName 2>&1 | tail -20`
Expected: All 4 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add synapse/federation/federation_server.py
git commit -m "feat(phase-10c): FederationServer uses _effective_server_name and _effective_signing_key"
```

---

## Task 7: Red probes for per-tenant federation allow-lists (sub-phase 10d)

**Files:**
- Modify: `tests/tenant/test_federation_inbound.py`

- [ ] **Step 1: Append 10d probes to the test file**

```python
# ── 10d probes: per-tenant federation allow-lists ────────────────


class TestTenantFederationConfig(TestCase):
    """TenantFederationConfig dataclass and integration with whitelist check."""

    def test_tenant_federation_config_from_dict(self) -> None:
        """TenantFederationConfig.from_dict parses a whitelist."""
        from synapse.config.tenants import TenantFederationConfig

        cfg = TenantFederationConfig.from_dict(
            {"federation_domain_whitelist": ["partner.com", "ally.org"]}
        )
        self.assertIn("partner.com", cfg.federation_domain_whitelist)
        self.assertIn("ally.org", cfg.federation_domain_whitelist)

    def test_tenant_federation_config_none_whitelist(self) -> None:
        """TenantFederationConfig with no whitelist means inherit global."""
        from synapse.config.tenants import TenantFederationConfig

        cfg = TenantFederationConfig.from_dict({})
        self.assertIsNone(cfg.federation_domain_whitelist)

    def test_tenant_config_has_federation_field(self) -> None:
        """TenantConfig must accept a federation field."""
        from synapse.config.tenants import TenantFederationConfig

        fed_cfg = TenantFederationConfig(
            federation_domain_whitelist={"partner.com": True}
        )
        tenant = TenantConfig(
            server_name="acme.localhost",
            database_schema="tenant_acme",
            signing_key_path="/keys/acme.key",
            media_store_path="/media/acme",
            federation=fed_cfg,
        )
        self.assertIs(tenant.federation, fed_cfg)

    def test_is_domain_allowed_tenant_override(self) -> None:
        """When tenant has whitelist, domain check uses tenant list, not global."""
        from synapse.config.federation import FederationConfig
        from synapse.config.tenants import TenantFederationConfig

        fed_config = FederationConfig(MagicMock())
        fed_config.federation_domain_whitelist = {"global.com": True}

        tenant_fed = TenantFederationConfig(
            federation_domain_whitelist={"tenant-only.com": True}
        )

        # tenant-only.com allowed by tenant, not by global
        self.assertTrue(
            fed_config.is_domain_allowed_according_to_federation_whitelist(
                "tenant-only.com", tenant_federation_config=tenant_fed
            )
        )
        # global.com NOT allowed by tenant whitelist
        self.assertFalse(
            fed_config.is_domain_allowed_according_to_federation_whitelist(
                "global.com", tenant_federation_config=tenant_fed
            )
        )

    def test_is_domain_allowed_falls_through_to_global(self) -> None:
        """When tenant has no whitelist (None), falls through to global."""
        from synapse.config.federation import FederationConfig
        from synapse.config.tenants import TenantFederationConfig

        fed_config = FederationConfig(MagicMock())
        fed_config.federation_domain_whitelist = {"global.com": True}

        tenant_fed = TenantFederationConfig(federation_domain_whitelist=None)

        self.assertTrue(
            fed_config.is_domain_allowed_according_to_federation_whitelist(
                "global.com", tenant_federation_config=tenant_fed
            )
        )
        self.assertFalse(
            fed_config.is_domain_allowed_according_to_federation_whitelist(
                "unknown.com", tenant_federation_config=tenant_fed
            )
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `trial tests.tenant.test_federation_inbound.TestTenantFederationConfig 2>&1 | tail -20`
Expected: FAIL — `TenantFederationConfig` doesn't exist, `is_domain_allowed` doesn't accept `tenant_federation_config`.

- [ ] **Step 3: Commit red probes**

```bash
git add tests/tenant/test_federation_inbound.py
git commit -m "test(phase-10d): add red probes for per-tenant federation allow-lists"
```

---

## Task 8: Implement per-tenant federation allow-lists (sub-phase 10d)

**Files:**
- Modify: `synapse/config/tenants.py`
- Modify: `synapse/config/federation.py:97-111`
- Modify: `synapse/federation/transport/server/_base.py` (Authenticator whitelist check)

- [ ] **Step 1: Add TenantFederationConfig dataclass**

In `synapse/config/tenants.py`, after the `TenantRatelimitConfig` class (around line 218), add:

```python
@attr.s(auto_attribs=True, slots=True, frozen=True)
class TenantFederationConfig:
    """Per-tenant federation configuration.

    When present on a TenantConfig, overrides the global federation
    domain whitelist for this tenant. When federation_domain_whitelist
    is None, the tenant inherits the global whitelist.
    """

    federation_domain_whitelist: dict[str, bool] | None = None

    @classmethod
    def from_dict(cls, d: "JsonDict") -> "TenantFederationConfig":
        whitelist_list = d.get("federation_domain_whitelist")
        whitelist: dict[str, bool] | None = None
        if whitelist_list is not None:
            whitelist = {domain: True for domain in whitelist_list}
        return cls(federation_domain_whitelist=whitelist)
```

- [ ] **Step 2: Add federation field to TenantConfig**

In `TenantConfig` (line 222), add after the `app_service_config_files` field (line 289):

```python
    # Per-tenant federation configuration. When set, overrides the global
    # federation domain whitelist. When None, the tenant inherits global
    # federation settings.
    federation: TenantFederationConfig | None = None
```

In `TenantConfig.from_dict` (line 332), add parsing before the `return cls(...)`:

```python
        federation_dict = config.get("federation")
        federation_cfg = (
            TenantFederationConfig.from_dict(federation_dict)
            if federation_dict
            else None
        )
```

And add `federation=federation_cfg` to the `return cls(...)` call.

- [ ] **Step 3: Make is_domain_allowed tenant-aware**

In `synapse/config/federation.py`, modify `is_domain_allowed_according_to_federation_whitelist` (line 97):

```python
    def is_domain_allowed_according_to_federation_whitelist(
        self,
        domain: str,
        tenant_federation_config: "TenantFederationConfig | None" = None,
    ) -> bool:
        """
        Returns whether a domain is allowed according to the federation whitelist.

        Resolution order:
        1. Tenant whitelist (if tenant_federation_config has a non-None whitelist)
        2. Global whitelist (if set)
        3. All domains allowed (both are None)

        Args:
            domain: The domain to test.
            tenant_federation_config: Optional per-tenant federation config.

        Returns:
            True if the domain is allowed, False otherwise.
        """
        # Tier 1: tenant-specific whitelist
        if tenant_federation_config is not None:
            tenant_wl = tenant_federation_config.federation_domain_whitelist
            if tenant_wl is not None:
                return domain in tenant_wl

        # Tier 2: global whitelist
        if self.federation_domain_whitelist is None:
            return True

        return domain in self.federation_domain_whitelist
```

Add `TYPE_CHECKING` import at top:

```python
if TYPE_CHECKING:
    from synapse.config.tenants import TenantFederationConfig
```

- [ ] **Step 4: Integrate tenant whitelist into Authenticator**

In `synapse/federation/transport/server/_base.py`, modify the whitelist check in `authenticate_request`. After `_resolve_federation_destination` sets tenant context, check the tenant whitelist:

```python
        # Resolve effective destination and set tenant context
        effective_destination = self._resolve_federation_destination(last_destination)
        json_request["destination"] = effective_destination

        # Check federation domain whitelist (tenant-aware)
        # This replaces the old global-only check:
        #   if self.federation_domain_whitelist is not None and origin not in ...
        tenant = get_current_tenant()
        tenant_fed_config = tenant.federation if tenant else None
        if not self._hs.config.federation.is_domain_allowed_according_to_federation_whitelist(
            origin, tenant_federation_config=tenant_fed_config
        ):
            raise FederationDeniedError(origin)
```

Note: `self._hs` was already added in Task 2 step 1. The old `self.federation_domain_whitelist` attribute and its direct check are removed — all whitelist logic goes through `is_domain_allowed_according_to_federation_whitelist`.

- [ ] **Step 5: Run tests to verify probes go green**

Run: `trial tests.tenant.test_federation_inbound.TestTenantFederationConfig 2>&1 | tail -20`
Expected: All 5 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add synapse/config/tenants.py synapse/config/federation.py synapse/federation/transport/server/_base.py
git commit -m "feat(phase-10d): per-tenant federation allow-lists with TenantFederationConfig"
```

---

## Task 9: Red probes for EventAuthHandler tenant-awareness (sub-phase 10e)

**Files:**
- Modify: `tests/tenant/test_federation_inbound.py`

- [ ] **Step 1: Append 10e probes to the test file**

```python
# ── 10e probes: EventAuthHandler tenant-awareness ────────────────


class TestEventAuthHandlerEffectiveServerName(TestCase):
    """EventAuthHandler._effective_server_name must resolve from tenant context."""

    def _make_handler(self, hostname: str = "main.localhost"):
        from synapse.handlers.event_auth import EventAuthHandler

        hs = MagicMock()
        hs.hostname = hostname
        hs.get_clock.return_value = MagicMock()
        hs.get_datastores.return_value.main = MagicMock()
        hs.get_storage_controllers.return_value.state = MagicMock()
        return EventAuthHandler(hs)

    def test_effective_server_name_with_tenant(self) -> None:
        """_effective_server_name returns tenant's server_name when context is set."""
        handler = self._make_handler()
        acme = _make_tenant("acme")
        with patch(
            "synapse.handlers.event_auth.get_current_tenant",
            return_value=acme,
        ):
            self.assertEqual(handler._effective_server_name, "acme.localhost")

    def test_effective_server_name_without_tenant(self) -> None:
        """_effective_server_name falls back to self._server_name when no context."""
        handler = self._make_handler()
        with patch(
            "synapse.handlers.event_auth.get_current_tenant",
            return_value=None,
        ):
            self.assertEqual(handler._effective_server_name, "main.localhost")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `trial tests.tenant.test_federation_inbound.TestEventAuthHandlerEffectiveServerName 2>&1 | tail -20`
Expected: FAIL — `_effective_server_name` doesn't exist on EventAuthHandler.

- [ ] **Step 3: Commit red probes**

```bash
git add tests/tenant/test_federation_inbound.py
git commit -m "test(phase-10e): add red probes for EventAuthHandler tenant-awareness"
```

---

## Task 10: Implement EventAuthHandler tenant-awareness (sub-phase 10e)

**Files:**
- Modify: `synapse/handlers/event_auth.py:55-58, 277`

- [ ] **Step 1: Add import and property**

Add import at top of `event_auth.py`:

```python
from synapse.tenant_context import get_current_tenant
```

After `__init__` (line 59), add the property:

```python
    @property
    def _effective_server_name(self) -> str:
        """Return the current tenant's server_name, or the global default."""
        tenant = get_current_tenant()
        return tenant.server_name if tenant else self._server_name
```

- [ ] **Step 2: Replace self._server_name at line 277**

Change line 277 from:

```python
                    if not await self._store.is_host_joined(room_id, self._server_name):
```

to:

```python
                    if not await self._store.is_host_joined(room_id, self._effective_server_name):
```

- [ ] **Step 3: Run tests to verify probes go green**

Run: `trial tests.tenant.test_federation_inbound.TestEventAuthHandlerEffectiveServerName 2>&1 | tail -20`
Expected: All 2 tests PASS.

- [ ] **Step 4: Run full probe suite**

Run: `trial tests.tenant.test_federation_inbound 2>&1 | tail -10`
Expected: All 20 probes PASS (6 + 3 + 4 + 5 + 2 = 20).

- [ ] **Step 5: Run existing federation tests for regression**

Run: `trial tests.tenant.test_federation_sender_multitenant 2>&1 | tail -5`
Expected: All existing tests PASS.

- [ ] **Step 6: Commit**

```bash
git add synapse/handlers/event_auth.py
git commit -m "feat(phase-10e): EventAuthHandler uses _effective_server_name for tenant-aware room membership checks"
```

---

## Task 11: Final verification and sync

- [ ] **Step 1: Run full tenant test suite**

Run: `trial tests.tenant 2>&1 | tail -20`
Expected: All tests pass including the new 20 probes.

- [ ] **Step 2: Verify no regressions in broader test suite**

Run: `trial tests.federation 2>&1 | tail -20`
Expected: No new failures.

- [ ] **Step 3: Commit any fixups if needed**

Only if previous steps revealed issues.
