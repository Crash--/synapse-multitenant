# Phase 10' — Bug Analysis

**Date:** 2026-04-10
**Status:** 10'-a **fixed (uncommitted)**; 10'-b **diagnosis only — design decisions needed**
**Branch:** `feature/multi-tenant`

---

## Bug 10'-a — Key server returns global keys for tenant hosts ✅ FIXED

### Root cause

`SynapseSite.__init__` (`synapse/http/site.py:1002-1017`) constructed its own
`TenantRegistry` instance from `hs.config.tenants.multi_tenant`:

```python
self._tenant_registry = TenantRegistry(tenants_config.multi_tenant)
```

In DB-driven mode (`source="database"`), the YAML tenants list is empty —
tenants live in the `public.tenants` table and are loaded into
`hs.get_tenant_registry()` at startup / on control-plane reload. The
site's private registry never saw them.

Consequence: `SynapseSite.get_tenant_for_host("matrix.tenant-a.com")`
returned `None` → `_setup_tenant_context` never set the ContextVar →
`LocalKey.on_GET` saw `get_current_tenant() == None` → fell through to
the global response (`server_name=localhost`, global signing key).

### Fix

Use the shared HS-level registry that is reloadable by the control plane:

```python
self._tenant_registry = hs.get_tenant_registry()
```

Also logs the source ("yaml" / "database") so operators can see at
startup which path is in play.

### Verification

- New probe `TestSynapseSiteUsesSharedTenantRegistry` in
  `tests/tenant/test_federation_inbound.py` — **21/21 pass**.
- Full federation test sweep — **50/50 pass** (`test_federation_inbound`,
  `test_federation_sender_multitenant`, `test_federation_sender`).
- Registry reload regression — **18/18 pass** (`test_registry`,
  `test_registry_reload`).
- Pre-existing failures in `tests.tenant.test_context` (23 errors) are
  due to outdated fixtures calling `reset_current_tenant()` without a
  token argument — **unrelated to this fix**.

### Files touched (uncommitted)

- `synapse/http/site.py` — replace YAML-bound registry with shared HS registry
- `tests/tenant/test_federation_inbound.py` — add `TestSynapseSiteUsesSharedTenantRegistry` probe

---

## Bug 10'-b — Cross-tenant room join fails ⚠ DIAGNOSIS ONLY

### Symptom

Bob on tenant-b tries to join a room created on tenant-a. The invite
succeeds (tenant-a → tenant-b), but the join from tenant-b's side fails
with HTTP 404 and:

> "Can't join remote room because no servers that are in the room have been provided."

### Root cause

The multi-tenant model has an architectural ambiguity: sibling tenants
are **"mine"** at the process level (same Synapse, same `is_mine_server_name`
returns True) but **"remote"** at the tenant-schema level (each tenant's
Postgres schema has separate room state).

The join path currently treats siblings as fully "mine", which breaks
cross-tenant joins in two places:

**Site 1 — inviter domain filtering (`synapse/handlers/room_member.py:1053-1055`):**

```python
inviter = await self._get_inviter(target.to_string(), room_id)
if inviter and not self.hs.is_mine(inviter):
    remote_room_hosts.append(inviter.domain)
```

For Bob on tenant-b joining a room from tenant-a:
- `inviter = @alice:matrix.tenant-a.com`
- `self.hs.is_mine(alice)` → True (tenant-a is a local tenant)
- `inviter.domain` is NOT added → `remote_room_hosts` stays empty

**Site 2 — `_is_host_in_room` (`room_member.py:1820-1822`):**

```python
for etype, state_key in partial_current_state_ids:
    if etype != EventTypes.Member or not self.hs.is_mine_id(state_key):
        continue
```

When checking if "the host" is in the room, this iterates the room's
current state. For Bob's join on tenant-b, the current state is read
from tenant-b's schema — which has no state for this room (the room
lives in tenant-a). Returns False → falls to the remote-join path.

**Site 3 — `_remote_join` self-filter (`room_member.py:1888-1890`):**

```python
remote_room_hosts = [
    host for host in remote_room_hosts if host != self.hs.hostname
]
```

Even if site 1 were fixed and `matrix.tenant-a.com` were in
`remote_room_hosts`, this filter only removes `hs.hostname`. If hostname
is `localhost`, tenant-a's host survives — good. But then
`do_invite_join` tries to send federation to `matrix.tenant-a.com`,
which triggers the `is_mine_server_name(dest)` guard in the transport
layer: **"Transport layer cannot send to itself!"**

### Design decisions needed before fixing

The minimal symptom fix (adding inviter domain to `remote_room_hosts`
when the inviter is a sibling tenant rather than the current tenant)
surfaces the deeper architectural question:

**How should cross-tenant room operations work?**

Three possible models, each with different implications:

#### Option A: Full federation loopback

Treat cross-tenant traffic as federation, looping back through the HTTP
stack. Tenant-b's join to tenant-a's room goes:

    tenant-b join → federation sender (tenant-b origin) →
    transport → HTTP self-connect → federation server (tenant-a destination) →
    tenant-a's state machine

**Pros:** Single code path. Mirrors production cross-server behavior
exactly. All existing event auth, state res, catchup logic works.

**Cons:** HTTP roundtrip for in-process cross-tenant calls (though this
is cheap over localhost). Requires fixing "send to self" guard to
distinguish "literally same tenant" from "sibling tenant".

**Change shape:** Narrow `is_mine_server_name` to check *current tenant*
only (not all tenants) in the transport-layer guard. Federation sender
already keys queues by `(tenant, destination)` from phase 9 — no
changes needed there.

#### Option B: In-process cross-tenant dispatch

Detect sibling destination, bypass HTTP, invoke the federation server's
handlers directly with a synthesized X-Matrix request. Same logical
path, no network hop.

**Pros:** Faster than A, avoids self-connect complexity.

**Cons:** New code path parallel to federation. Risk of behavioral
drift between "real federation" and "in-process federation". Still
needs tenant-aware guards everywhere.

#### Option C: Explicitly forbid cross-tenant rooms

Sibling tenants cannot share rooms. If alice@tenant-a invites
bob@tenant-b, the invite fails at creation time.

**Pros:** No architectural work. Clean isolation boundary.

**Cons:** Breaks the demo and any use case that wants multi-tenant with
cross-domain collaboration. Changes the product.

### Recommendation

**Option A** matches the principle that tenants are isolated homeservers
that happen to share a process. All phase 9 outbound work was built on
this model (per-tenant queues keyed by `(tenant_server_name,
destination)`, per-tenant signing). Cross-tenant delivery is just
"federation where the destination happens to be a sibling on the same
process" — the existing federation machinery is designed for this.

The scope is:

1. **Transport self-connect.** When `FederationSender` tries to send to
   a sibling tenant, it must actually send (via localhost HTTPS or a
   shortcut). Currently `transport/client.py:297` raises
   `"Transport layer cannot send to itself!"` whenever
   `_is_mine_server_name(destination)` is True. Change to:
   `destination == current_tenant.server_name` (i.e. only reject if the
   destination IS the current tenant).
2. **Join-path filtering.** `room_member.py:1053` and similar sites use
   `is_mine` to mean "current tenant" but treat it as "any tenant in
   this process". Introduce `is_mine_for_current_tenant(domain)` (or
   similar) that returns True only when `domain == current_tenant.server_name`.
3. **`_remote_join` self-filter** (`room_member.py:1888-1890`) —
   replace `host != self.hs.hostname` with `host != current_tenant.server_name`.
4. **`_is_host_in_room`** (`room_member.py:1820-1822`) — replace
   `self.hs.is_mine_id(state_key)` with a current-tenant-scoped check.
5. **Federation server destination guard** (`transport/server/_base.py`)
   — the tenant ContextVar set by the authenticator (phase 10a) already
   handles this correctly for inbound. Verify no other guard compares
   `destination` against a global hostname for "self".

### Scope caveat

This touches ~5 call sites but each affects the join/leave/invite paths
for all rooms. It needs careful test coverage:

- Room created on tenant-a, user invited from tenant-b, join succeeds.
- Room created on tenant-a, user on tenant-a joins — still uses local
  path (no federation loopback).
- Room created on tenant-a, user from external server joins — unchanged.
- Leave, ban, kick across tenants.

Recommend following phase-9's structure: write red probes first, commit
a design spec, then implement behind a subagent-driven plan with sub-phases.

---

## Summary for the user

- **10'-a fixed, uncommitted.** One-line change plus new probe.
  50/50 related tests pass. Ready to commit.
- **10'-b requires a design decision (A/B/C above).** Not autonomous
  territory — the choice affects the product (does multi-tenant mean
  "shared platform with cross-tenant rooms" or "isolated homeservers
  that happen to share a process"?). Diagnosis and recommended approach
  (Option A) above, but I did not implement.
