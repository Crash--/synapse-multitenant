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
from io import StringIO
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
        self._tenants = dict(multi_tenant_config.tenants)
        self._inactive_tenants: set[str] = set()
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

    def reload(
        self, new_config: MultiTenantConfig
    ) -> dict[str, list[str]]:
        """Reload the registry with a new configuration.

        Performs an in-place update so cached references (e.g. via
        ``@cache_in_self`` on ``HomeServer``) remain valid.

        - Tenants present in new_config but not currently active are added
          (and reactivated if previously inactive).
        - Tenants absent from new_config become inactive.
        - Unchanged tenants keep their existing state.

        Args:
            new_config: The new multi-tenant configuration.

        Returns:
            A dict with ``added``, ``removed``, and ``unchanged`` lists
            of server names.
        """
        new_names = set(new_config.tenants.keys())
        old_active = set(self._tenants.keys()) - self._inactive_tenants

        added: list[str] = []
        removed: list[str] = []
        unchanged: list[str] = []

        # Determine added / reactivated tenants
        for name in sorted(new_names):
            if name in old_active:
                # Update the config in place for unchanged tenants
                self._tenants[name] = new_config.tenants[name]
                unchanged.append(name)
            else:
                # New or reactivated
                self._tenants[name] = new_config.tenants[name]
                self._inactive_tenants.discard(name)
                self._signing_keys.pop(name, None)
                added.append(name)

        # Determine removed tenants
        for name in sorted(old_active - new_names):
            self._inactive_tenants.add(name)
            removed.append(name)

        logger.info(
            "Tenant registry reloaded: added=%s removed=%s unchanged=%s",
            added,
            removed,
            unchanged,
        )

        return {"added": added, "removed": removed, "unchanged": unchanged}

    def is_inactive(self, server_name: str) -> bool:
        """Check if a tenant is inactive (removed via reload).

        Args:
            server_name: The server name to check.

        Returns:
            True if the tenant exists but is inactive, False otherwise.
        """
        return server_name in self._inactive_tenants

    def get_tenant(self, server_name: str) -> TenantConfig | None:
        """Get the tenant configuration for a given server name.

        Args:
            server_name: The Matrix server name to look up.

        Returns:
            The TenantConfig if found and active, None otherwise.
        """
        if server_name in self._inactive_tenants:
            return None

        # First check direct mapping
        tenant = self._tenants.get(server_name)
        if tenant:
            return tenant

        # Check hostname aliases
        aliased_name = self._hostname_aliases.get(server_name)
        if aliased_name and aliased_name not in self._inactive_tenants:
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
        """Get all configured active tenants.

        Returns:
            List of all active TenantConfig instances.
        """
        return [
            t
            for name, t in self._tenants.items()
            if name not in self._inactive_tenants
        ]

    def get_all_server_names(self) -> list[str]:
        """Get all configured active server names.

        Returns:
            List of all active tenant server names.
        """
        return [
            name
            for name in self._tenants.keys()
            if name not in self._inactive_tenants
        ]

    def is_local_server_name(self, server_name: str) -> bool:
        """Check if a server name belongs to a local tenant.

        This is used to determine if federation requests are "local"
        (between tenants on the same Synapse instance).

        Args:
            server_name: The server name to check.

        Returns:
            True if the server name is a local tenant, False otherwise.
        """
        if server_name in self._inactive_tenants:
            return False
        if server_name in self._tenants:
            return True
        aliased = self._hostname_aliases.get(server_name)
        if aliased and aliased not in self._inactive_tenants:
            return True
        return False

    def add_hostname_alias(self, alias: str, server_name: str) -> None:
        """Add a hostname alias for a tenant.

        This allows a tenant to be reached via multiple hostnames.

        Args:
            alias: The alias hostname.
            server_name: The actual tenant server name.

        Raises:
            TenantNotFoundError: If the target server_name doesn't exist.
        """
        if server_name not in self._tenants or server_name in self._inactive_tenants:
            raise TenantNotFoundError(server_name)
        self._hostname_aliases[alias] = server_name
        logger.info("Added hostname alias: %s -> %s", alias, server_name)

    def load_signing_key(self, server_name: str) -> list:
        """Load and cache the signing key for a tenant.

        Supports two sources:
        - Filesystem: when ``tenant.signing_key_path`` is set.
        - In-memory: when ``tenant.signing_key_data`` is set (DB-sourced).

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

        if tenant.signing_key_data is not None:
            # DB-sourced key: parse from in-memory string
            keys = read_signing_keys(StringIO(tenant.signing_key_data))
            self._signing_keys[server_name] = keys
            logger.info(
                "Loaded signing key for tenant %s from database",
                server_name,
            )
            return keys

        key_path = tenant.signing_key_path
        if key_path is None:
            raise ValueError(
                f"Tenant {server_name} has neither signing_key_path nor signing_key_data"
            )
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
        for server_name in self.get_all_server_names():
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


def load_tenants_from_database(
    db_conn,
    master_key: bytes | None = None,
    default_schema: str = "public",
) -> MultiTenantConfig:
    """Load tenant configurations from the ``public.tenants`` table.

    Args:
        db_conn: A DB-API 2 connection (e.g. ``psycopg2.connection``).
        master_key: AES-256-GCM master key for decrypting signing keys.
            Required when tenants have encrypted keys in the database.
        default_schema: The default schema name.

    Returns:
        A ``MultiTenantConfig`` with ``source="database"`` containing all
        active tenants.
    """
    cursor = db_conn.cursor()
    cursor.execute(
        "SELECT * FROM public.tenants WHERE status = 'active'"
    )
    columns = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    cursor.close()

    tenants: dict[str, TenantConfig] = {}

    for row_tuple in rows:
        row = dict(zip(columns, row_tuple))

        # Decrypt signing key if encrypted column is present
        encrypted_key = row.get("signing_key_encrypted")
        if encrypted_key is not None and master_key is not None:
            from synapse.crypto.tenant_key_encryption import decrypt_signing_key
            row["signing_key_data"] = decrypt_signing_key(
                encrypted_key, master_key
            )
        elif row.get("signing_key_data") is None:
            logger.warning(
                "Tenant %s has no signing key data and no encrypted key",
                row.get("server_name"),
            )
            continue

        # Parse JSONB columns — psycopg2 returns them as dicts already
        tenant = TenantConfig.from_db_row(row)
        tenants[tenant.server_name] = tenant
        logger.info(
            "Loaded tenant %s (schema: %s) from database",
            tenant.server_name,
            tenant.database_schema,
        )

    return MultiTenantConfig(
        enabled=True,
        default_schema=default_schema,
        source="database",
        tenants=tenants,
    )


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
