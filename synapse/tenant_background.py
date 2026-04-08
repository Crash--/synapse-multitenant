#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
#

"""Per-tenant scheduling helpers for background processes.

Synapse's `run_as_background_process` is the standard way to fire-and-forget
work outside the request lifecycle (looping calls, notify-driven loops,
push pumps, retention sweeps, …). On a single-tenant homeserver that's
fine — the process inherits the global `hs.hostname`, the global media
root, the public DB schema, and that's all there is.

In multi-tenant mode there is no single tenant for a background process to
"belong to" — most loops semantically need to run *once per tenant*, with
the per-request `TenantConfig` ContextVar bound so that:

- `synapse/storage/database.py` captures the tenant via `get_current_tenant()`
  and sets `search_path` to the tenant's schema before running SQL
- `synapse/http/site.py` and other helpers route signing/media/etc. through
  the active tenant
- `LoggingContext.server_name` and the Prometheus `server_name` label
  reflect the tenant the loop iteration is operating on

`run_as_background_process_per_tenant` is the canonical wrapper for this:
iterate the configured tenants, bind each one with `tenant_context()`,
and schedule one independent background process per tenant. Each iteration
gets its own `BackgroundProcessLoggingContext`, its own metric labels, and
fails independently of the others.

If multi-tenant mode is disabled the helper falls through to a single
`run_as_background_process` call labelled with `hs.hostname`, so callsites
don't need an `if multi_tenant:` switch.
"""

from typing import TYPE_CHECKING, Any, Awaitable, Callable, TypeVar

from twisted.internet import defer

from synapse.metrics.background_process_metrics import run_as_background_process
from synapse.tenant_context import tenant_context

if TYPE_CHECKING:
    from typing_extensions import LiteralString

    from synapse.server import HomeServer

R = TypeVar("R")


def run_as_background_process_per_tenant(
    desc: "LiteralString",
    hs: "HomeServer",
    func: Callable[..., Awaitable[R | None]],
    *args: Any,
    **kwargs: Any,
) -> "list[defer.Deferred[R | None]]":
    """Schedule `func` as a background process once per configured tenant.

    For each tenant in the registry this function:
      1. Sets the tenant `ContextVar` (so anything reaching for
         `get_current_tenant()` downstream — DB schema selection, signing
         keys, media paths, log context — sees the right tenant).
      2. Calls `run_as_background_process(desc, tenant.server_name, …)`,
         which gives that iteration its own `LoggingContext`, its own
         metric series, and isolated error handling.

    The tenant context is bound *inside* the wrapped coroutine, not before
    `run_as_background_process` returns, because contextvars set in the
    parent before scheduling would not propagate cleanly through Twisted's
    `defer.ensureDeferred` boundary. Setting it inside the coroutine
    ensures every nested `await` sees the right tenant.

    When multi-tenant mode is disabled (or no tenants are configured) the
    helper degrades to a single `run_as_background_process` call labelled
    with `hs.hostname`, so callsites can switch to it unconditionally.

    Args:
        desc: Background process description (used for the metric `name`
            label and the logging context name). Must be a literal string
            so it has bounded cardinality.
        hs: HomeServer instance — used to reach the tenant registry and
            to provide the fallback `hostname` label.
        func: Coroutine function to run. It is called once per tenant
            with the same `*args, **kwargs`. The active tenant is
            available via `synapse.tenant_context.get_current_tenant()`.
        *args, **kwargs: Forwarded verbatim to `func` on every iteration.

    Returns:
        A list of `Deferred`s, one per scheduled iteration (one element
        in the single-tenant fallback case). Callers that don't care
        about the result — the common case for `looping_call` and
        notify-driven loops — can ignore the return value.
    """
    registry = hs.get_tenant_registry()

    # Single-tenant / disabled fallback. Note we don't bind a tenant
    # context here because there is no `TenantConfig` to bind — the
    # downstream DB layer falls back to its existing behaviour
    # (`search_path = public`), which is what a single-tenant deployment
    # already does.
    if not registry.enabled:
        return [
            run_as_background_process(desc, hs.hostname, func, *args, **kwargs)
        ]

    tenants = registry.get_all_tenants()
    if not tenants:
        # Multi-tenant enabled but no tenants configured — extreme corner
        # case (config validation should have rejected this). Be loud
        # rather than silently doing nothing.
        return [
            run_as_background_process(desc, hs.hostname, func, *args, **kwargs)
        ]

    deferreds: "list[defer.Deferred[R | None]]" = []
    for tenant in tenants:
        # Bind `tenant` as a default arg so the closure captures the
        # current loop value, not the loop variable itself.
        async def _runner(_t=tenant) -> R | None:
            with tenant_context(_t):
                return await func(*args, **kwargs)

        deferreds.append(
            run_as_background_process(desc, tenant.server_name, _runner)
        )
    return deferreds
