#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

"""Per-tenant rate limiter registry."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, Optional

from synapse.api.ratelimiting import Ratelimiter
from synapse.config.ratelimiting import RatelimitSettings

if TYPE_CHECKING:
    from synapse.config.tenants import TenantConfig
    from synapse.storage.databases.main import DataStore
    from synapse.tenant_registry import TenantRegistry
    from synapse.util import Clock

logger = logging.getLogger(__name__)

_GLOBAL = "__global__"


class TenantRatelimiterRegistry:
    """Caches per-tenant Ratelimiter instances, creating them lazily."""

    def __init__(
        self,
        store: "DataStore",
        clock: "Clock",
        global_settings: dict[str, RatelimitSettings],
    ) -> None:
        self._store = store
        self._clock = clock
        self._global_settings = global_settings
        self._cache: dict[tuple[str, str], Ratelimiter] = {}

    def get(
        self,
        limiter_key: str,
        tenant: "TenantConfig | None" = None,
        ratelimit_callbacks: Optional[Callable] = None,
    ) -> Ratelimiter:
        """Return the Ratelimiter for the given limiter key and tenant.

        Args:
            limiter_key: The config key identifying which limiter to use (e.g.
                ``"rc_joins_local"``).
            tenant: The tenant context for this request. If *None* (or the tenant
                has no per-tenant ratelimit overrides) the global settings are used.
            ratelimit_callbacks: Optional module-API ratelimit callback.  When
                provided the returned ``Ratelimiter`` is **not** cached because the
                callback reference is bound to a specific handler instance and may
                differ across call sites.
        """
        tenant_setting = None
        if tenant is not None and tenant.ratelimit is not None:
            tenant_setting = getattr(tenant.ratelimit, limiter_key, None)

        if tenant_setting is not None:
            cache_key = (tenant.server_name, limiter_key)
            settings = tenant_setting
        else:
            cache_key = (_GLOBAL, limiter_key)
            settings = self._global_settings.get(limiter_key)
            if settings is None:
                raise KeyError(
                    f"No global RatelimitSettings for limiter key {limiter_key!r}"
                )

        # When callbacks are provided we skip the cache to avoid sharing a
        # Ratelimiter that has a different callback reference.
        if ratelimit_callbacks is not None:
            return Ratelimiter(
                store=self._store,
                clock=self._clock,
                cfg=settings,
                ratelimit_callbacks=ratelimit_callbacks,
            )

        if cache_key not in self._cache:
            self._cache[cache_key] = Ratelimiter(
                store=self._store,
                clock=self._clock,
                cfg=settings,
            )

        return self._cache[cache_key]

    def reload(self, registry: "TenantRegistry") -> None:
        """Clear cached limiters for tenants no longer active.

        New tenants don't need setup — their limiters are lazy-created via get().
        """
        active_names = set(t.server_name for t in registry.get_all_tenants())
        for key in list(self._cache.keys()):
            server_name, _ = key
            if server_name != _GLOBAL and server_name not in active_names:
                del self._cache[key]
                logger.info(
                    "Cleared cached rate limiter for inactive tenant: %s", server_name
                )
