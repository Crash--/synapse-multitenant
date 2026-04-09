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
Multi-tenant keyring for Synapse.

This module provides a multi-tenant aware keyring that manages signing keys
for multiple tenants. Each tenant has its own signing key, and the keyring
ensures that events and requests are signed with the correct key.
"""

import logging
from typing import TYPE_CHECKING

from signedjson.key import get_verify_key, read_signing_keys
from signedjson.sign import sign_json
from signedjson.types import SigningKey

from synapse.storage.keys import FetchKeyResult
from synapse.tenant_context import get_current_tenant

if TYPE_CHECKING:
    from synapse.config.tenants import TenantConfig
    from synapse.server import HomeServer
    from synapse.tenant_registry import TenantRegistry

logger = logging.getLogger(__name__)


class MultiTenantKeyring:
    """Manages signing keys for multiple tenants.

    This class loads and caches signing keys for all configured tenants,
    providing methods to sign JSON objects with the appropriate tenant key.

    In multi-tenant mode, this class should be used instead of directly
    accessing the HomeServer's signing key.

    Attributes:
        _signing_keys: Dictionary mapping server_name to list of signing keys
        _verify_keys: Dictionary mapping server_name to FetchKeyResult
    """

    def __init__(self, hs: "HomeServer", tenant_registry: "TenantRegistry") -> None:
        """Initialize the multi-tenant keyring.

        Args:
            hs: The HomeServer instance.
            tenant_registry: The tenant registry with tenant configurations.
        """
        self._hs = hs
        self._registry = tenant_registry
        self._signing_keys: dict[str, list[SigningKey]] = {}
        self._verify_keys: dict[str, dict[str, FetchKeyResult]] = {}

        # Load keys for all tenants on initialization
        self._load_all_keys()

    def _load_all_keys(self) -> None:
        """Load signing keys for all configured tenants."""
        for tenant in self._registry.get_all_tenants():
            try:
                self._load_tenant_keys(tenant)
            except Exception as e:
                logger.error(
                    "Failed to load signing keys for tenant %s: %s",
                    tenant.server_name,
                    e,
                )
                raise

    def _load_tenant_keys(self, tenant: "TenantConfig") -> None:
        """Load signing keys for a specific tenant.

        Args:
            tenant: The tenant configuration.
        """
        key_path = tenant.signing_key_path

        try:
            with open(key_path, "r") as f:
                keys = read_signing_keys(f)
        except FileNotFoundError:
            raise FileNotFoundError(
                f"Signing key file not found for tenant {tenant.server_name}: {key_path}"
            )

        self._signing_keys[tenant.server_name] = keys

        # Also build verify key results for local key lookups
        verify_keys: dict[str, FetchKeyResult] = {}
        for key in keys:
            vk = get_verify_key(key)
            key_id = f"{vk.alg}:{vk.version}"
            verify_keys[key_id] = FetchKeyResult(
                verify_key=vk,
                valid_until_ts=2**63,  # Far future timestamp
            )
        self._verify_keys[tenant.server_name] = verify_keys

        logger.info(
            "Loaded %d signing key(s) for tenant %s from %s",
            len(keys),
            tenant.server_name,
            key_path,
        )

    def get_signing_key(self, server_name: str) -> SigningKey:
        """Get the primary signing key for a server name.

        Args:
            server_name: The Matrix server name.

        Returns:
            The signing key for the server.

        Raises:
            KeyError: If no signing key is loaded for the server.
        """
        keys = self._signing_keys.get(server_name)
        if not keys:
            raise KeyError(f"No signing key loaded for server: {server_name}")
        return keys[0]

    def get_current_signing_key(self) -> SigningKey:
        """Get the signing key for the current tenant context.

        Returns:
            The signing key for the current tenant.

        Raises:
            RuntimeError: If no tenant context is set.
            KeyError: If no signing key is loaded for the tenant.
        """
        tenant = get_current_tenant()
        if tenant is None:
            # Fall back to the main server's signing key
            return self._hs.signing_key
        return self.get_signing_key(tenant.server_name)

    def get_all_signing_keys(self, server_name: str) -> list[SigningKey]:
        """Get all signing keys for a server name.

        Args:
            server_name: The Matrix server name.

        Returns:
            List of signing keys for the server.

        Raises:
            KeyError: If no signing keys are loaded for the server.
        """
        keys = self._signing_keys.get(server_name)
        if not keys:
            raise KeyError(f"No signing keys loaded for server: {server_name}")
        return keys

    def get_verify_keys(self, server_name: str) -> dict[str, FetchKeyResult]:
        """Get the verify keys for a server name.

        This is used for key lookups when verifying signatures from local tenants.

        Args:
            server_name: The Matrix server name.

        Returns:
            Dictionary mapping key_id to FetchKeyResult.
        """
        return self._verify_keys.get(server_name, {})

    def sign_json_for_server(
        self, json_object: dict, server_name: str
    ) -> dict:
        """Sign a JSON object for a specific server.

        Args:
            json_object: The JSON object to sign.
            server_name: The server name to sign as.

        Returns:
            The signed JSON object.

        Raises:
            KeyError: If no signing key is loaded for the server.
        """
        key = self.get_signing_key(server_name)
        return sign_json(json_object, server_name, key)

    def sign_json_for_current_tenant(self, json_object: dict) -> dict:
        """Sign a JSON object for the current tenant context.

        Args:
            json_object: The JSON object to sign.

        Returns:
            The signed JSON object.

        Raises:
            RuntimeError: If no tenant context is set.
            KeyError: If no signing key is loaded for the tenant.
        """
        tenant = get_current_tenant()
        if tenant is None:
            # Fall back to main server signing
            return sign_json(
                json_object, self._hs.hostname, self._hs.signing_key
            )
        return self.sign_json_for_server(json_object, tenant.server_name)

    def is_local_server_name(self, server_name: str) -> bool:
        """Check if a server name is a local tenant.

        Args:
            server_name: The server name to check.

        Returns:
            True if the server name is a local tenant, False otherwise.
        """
        return server_name in self._signing_keys

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

    def reload_tenant_keys(self, server_name: str) -> None:
        """Reload signing keys for a specific tenant.

        This can be used to reload keys after they've been rotated.

        Args:
            server_name: The tenant server name to reload keys for.

        Raises:
            TenantNotFoundError: If the tenant is not found.
        """
        tenant = self._registry.get_tenant_or_raise(server_name)
        self._load_tenant_keys(tenant)
        logger.info("Reloaded signing keys for tenant %s", server_name)


def create_multi_tenant_keyring(hs: "HomeServer") -> MultiTenantKeyring | None:
    """Create a multi-tenant keyring if multi-tenant mode is enabled.

    Args:
        hs: The HomeServer instance.

    Returns:
        A MultiTenantKeyring instance, or None if multi-tenant mode is disabled.
    """
    tenants_config = getattr(hs.config, "tenants", None)
    if tenants_config is None or not tenants_config.multi_tenant.enabled:
        return None

    from synapse.tenant_registry import TenantRegistry

    registry = TenantRegistry(tenants_config.multi_tenant)
    return MultiTenantKeyring(hs, registry)
