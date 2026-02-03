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

"""
Tenant registry for multi-tenant Synapse.

This module provides a central registry for managing tenant configurations,
including loading tenants from configuration files and providing lookup
functionality by hostname.

The registry also handles:
- Loading and caching signing keys for each tenant
- Validating tenant configurations
- Providing access to tenant-specific resources
"""

import logging
import os
from typing import TYPE_CHECKING

from signedjson.key import read_signing_keys

from synapse.config.tenants import MultiTenantConfig, TenantConfig

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class TenantNotFoundError(Exception):
    """Raised when a requested tenant is not found in the registry."""

    def __init__(self, server_name: str) -> None:
        self.server_name = server_name
        super().__init__(f"Tenant not found: {server_name}")


class TenantRegistry:
    """Central registry for tenant configurations.

    The TenantRegistry is the authoritative source for tenant information
    in a multi-tenant Synapse deployment. It loads tenant configurations
    from the Synapse config file and provides lookup functionality.

    Attributes:
        enabled: Whether multi-tenant mode is enabled
        tenants: Dictionary mapping server_name to TenantConfig
    """

    def __init__(self, multi_tenant_config: MultiTenantConfig) -> None:
        """Initialize the tenant registry.

        Args:
            multi_tenant_config: The multi-tenant configuration from Synapse config.
        """
        self._config = multi_tenant_config
        self._tenants = multi_tenant_config.tenants
        self._signing_keys: dict[str, list] = {}
        self._hostname_aliases: dict[str, str] = {}

        if self.enabled:
            logger.info(
                "Tenant registry initialized with %d tenants: %s",
                len(self._tenants),
                list(self._tenants.keys()),
            )

    @property
    def enabled(self) -> bool:
        """Whether multi-tenant mode is enabled."""
        return self._config.enabled

    @property
    def default_schema(self) -> str:
        """The default database schema for shared data."""
        return self._config.default_schema

    def get_tenant(self, server_name: str) -> TenantConfig | None:
        """Get the tenant configuration for a given server name.

        Args:
            server_name: The Matrix server name to look up.

        Returns:
            The TenantConfig if found, None otherwise.
        """
        # First check direct mapping
        tenant = self._tenants.get(server_name)
        if tenant:
            return tenant

        # Check hostname aliases
        aliased_name = self._hostname_aliases.get(server_name)
        if aliased_name:
            return self._tenants.get(aliased_name)

        return None

    def get_tenant_or_raise(self, server_name: str) -> TenantConfig:
        """Get the tenant configuration, raising if not found.

        Args:
            server_name: The Matrix server name to look up.

        Returns:
            The TenantConfig for the server name.

        Raises:
            TenantNotFoundError: If the tenant is not found.
        """
        tenant = self.get_tenant(server_name)
        if tenant is None:
            raise TenantNotFoundError(server_name)
        return tenant

    def get_all_tenants(self) -> list[TenantConfig]:
        """Get all configured tenants.

        Returns:
            List of all TenantConfig instances.
        """
        return list(self._tenants.values())

    def get_all_server_names(self) -> list[str]:
        """Get all configured server names.

        Returns:
            List of all tenant server names.
        """
        return list(self._tenants.keys())

    def is_local_server_name(self, server_name: str) -> bool:
        """Check if a server name belongs to a local tenant.

        This is used to determine if federation requests are "local"
        (between tenants on the same Synapse instance).

        Args:
            server_name: The server name to check.

        Returns:
            True if the server name is a local tenant, False otherwise.
        """
        return server_name in self._tenants or server_name in self._hostname_aliases

    def add_hostname_alias(self, alias: str, server_name: str) -> None:
        """Add a hostname alias for a tenant.

        This allows a tenant to be reached via multiple hostnames.

        Args:
            alias: The alias hostname.
            server_name: The actual tenant server name.

        Raises:
            TenantNotFoundError: If the target server_name doesn't exist.
        """
        if server_name not in self._tenants:
            raise TenantNotFoundError(server_name)
        self._hostname_aliases[alias] = server_name
        logger.info("Added hostname alias: %s -> %s", alias, server_name)

    def load_signing_key(self, server_name: str) -> list:
        """Load and cache the signing key for a tenant.

        Args:
            server_name: The tenant server name.

        Returns:
            List of signing keys for the tenant.

        Raises:
            TenantNotFoundError: If the tenant is not found.
            FileNotFoundError: If the signing key file doesn't exist.
        """
        if server_name in self._signing_keys:
            return self._signing_keys[server_name]

        tenant = self.get_tenant_or_raise(server_name)

        key_path = tenant.signing_key_path
        if not os.path.exists(key_path):
            raise FileNotFoundError(
                f"Signing key file not found for tenant {server_name}: {key_path}"
            )

        with open(key_path, "r") as f:
            keys = read_signing_keys(f)

        self._signing_keys[server_name] = keys
        logger.info("Loaded signing key for tenant %s from %s", server_name, key_path)
        return keys

    def get_signing_key(self, server_name: str) -> list:
        """Get the signing key for a tenant, loading if necessary.

        Args:
            server_name: The tenant server name.

        Returns:
            List of signing keys for the tenant.
        """
        if server_name not in self._signing_keys:
            return self.load_signing_key(server_name)
        return self._signing_keys[server_name]

    def preload_all_signing_keys(self) -> None:
        """Preload signing keys for all tenants.

        This is useful during startup to ensure all keys are valid
        and loaded into memory.
        """
        for server_name in self._tenants:
            try:
                self.load_signing_key(server_name)
            except Exception as e:
                logger.error(
                    "Failed to load signing key for tenant %s: %s",
                    server_name,
                    e,
                )
                raise

    def get_media_store_path(self, server_name: str) -> str:
        """Get the media store path for a tenant.

        Args:
            server_name: The tenant server name.

        Returns:
            The media store path for the tenant.

        Raises:
            TenantNotFoundError: If the tenant is not found.
        """
        tenant = self.get_tenant_or_raise(server_name)
        return tenant.media_store_path

    def get_database_schema(self, server_name: str) -> str:
        """Get the database schema for a tenant.

        Args:
            server_name: The tenant server name.

        Returns:
            The database schema name for the tenant.

        Raises:
            TenantNotFoundError: If the tenant is not found.
        """
        tenant = self.get_tenant_or_raise(server_name)
        return tenant.database_schema


def create_tenant_registry(hs: "HomeServer") -> TenantRegistry:
    """Create a tenant registry from HomeServer configuration.

    Args:
        hs: The HomeServer instance.

    Returns:
        A configured TenantRegistry instance.
    """
    # Access the tenants config section
    tenants_config = getattr(hs.config, "tenants", None)
    if tenants_config is None:
        # Multi-tenant not configured, return disabled registry
        return TenantRegistry(MultiTenantConfig(enabled=False))

    return TenantRegistry(tenants_config.multi_tenant)
