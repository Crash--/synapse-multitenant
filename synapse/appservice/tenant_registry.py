#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

"""Per-tenant app service registry."""

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
            if namespace.exclusive:
                patterns.append(namespace.regex.pattern)
    if not patterns:
        return None
    return re.compile("|".join("(?:%s)" % p for p in patterns))


class TenantAppServiceRegistry:
    """Holds per-tenant ApplicationService lists."""

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
            if str(service.sender) == user_id:
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
        return list(self._all_services)
