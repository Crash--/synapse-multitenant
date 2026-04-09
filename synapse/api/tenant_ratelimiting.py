#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

"""Per-tenant rate limiter registry."""

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
        self, limiter_key: str, tenant: "TenantConfig | None" = None
    ) -> Ratelimiter:
        """Return the Ratelimiter for the given limiter key and tenant."""
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

        if cache_key not in self._cache:
            self._cache[cache_key] = Ratelimiter(
                store=self._store,
                clock=self._clock,
                cfg=settings,
            )

        return self._cache[cache_key]
