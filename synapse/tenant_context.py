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
Tenant context management for multi-tenant Synapse.

This module provides thread-local (context-local) storage for the current
tenant configuration, allowing code throughout Synapse to access tenant-specific
settings without passing tenant information through every function call.

The implementation uses Python's contextvars module, which provides proper
isolation for async code and is compatible with Twisted's async model.

Usage:
    from synapse.tenant_context import get_current_tenant, set_current_tenant

    # In request middleware:
    set_current_tenant(tenant_config)

    # Anywhere in the codebase:
    tenant = get_current_tenant()
    if tenant:
        schema = tenant.database_schema
"""

import contextvars
import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from synapse.config.tenants import TenantConfig

logger = logging.getLogger(__name__)

# Context variable to hold the current tenant configuration
# This is automatically scoped to the current async context/coroutine
_current_tenant: contextvars.ContextVar["TenantConfig | None"] = contextvars.ContextVar(
    "current_tenant", default=None
)


def get_current_tenant() -> "TenantConfig | None":
    """Get the current tenant configuration for this request context.

    Returns:
        The TenantConfig for the current request, or None if not in a
        multi-tenant context or no tenant has been set.
    """
    return _current_tenant.get()


def get_current_tenant_or_raise() -> "TenantConfig":
    """Get the current tenant configuration, raising if not set.

    Returns:
        The TenantConfig for the current request.

    Raises:
        RuntimeError: If no tenant context has been set.
    """
    tenant = _current_tenant.get()
    if tenant is None:
        raise RuntimeError(
            "No tenant context set. This code path requires a tenant context "
            "but was called outside of a tenant-scoped request."
        )
    return tenant


def set_current_tenant(tenant: "TenantConfig | None") -> contextvars.Token["TenantConfig | None"]:
    """Set the current tenant configuration for this request context.

    Args:
        tenant: The TenantConfig to set as current, or None to clear.

    Returns:
        A token that can be used to restore the previous value.
    """
    return _current_tenant.set(tenant)


def get_effective_server_name(fallback: str) -> str:
    """Get the effective server_name for the current context.

    Returns the current tenant's server_name if a tenant context is set,
    otherwise the fallback (typically ``hs.hostname``).

    Distinct from ``hs.is_mine_server_name``, which returns True for ANY
    local tenant. Use this helper when the question is "is this the
    *current* tenant?" — e.g. deciding whether a destination is literally
    self-traffic (reject) versus sibling-tenant traffic (valid federation
    even though it's same-process).

    Args:
        fallback: The value to return when no tenant context is set.

    Returns:
        The current tenant's ``server_name`` if set, else ``fallback``.
    """
    tenant = _current_tenant.get()
    return tenant.server_name if tenant is not None else fallback


def reset_current_tenant(token: contextvars.Token["TenantConfig | None"]) -> None:
    """Reset the current tenant to its previous value using a token.

    Args:
        token: The token returned from a previous set_current_tenant call.
    """
    _current_tenant.reset(token)


@contextmanager
def tenant_context(tenant: "TenantConfig") -> Iterator[None]:
    """Context manager to temporarily set the current tenant.

    This is useful for running code in the context of a specific tenant,
    ensuring the tenant context is properly cleaned up afterwards.

    Args:
        tenant: The TenantConfig to use within this context.

    Yields:
        None

    Example:
        with tenant_context(tenant_config):
            # Code here runs with tenant_config as the current tenant
            do_something()
        # Original tenant context is restored here
    """
    token = set_current_tenant(tenant)
    try:
        yield
    finally:
        reset_current_tenant(token)


def get_current_server_name() -> str | None:
    """Get the server name for the current tenant context.

    This is a convenience function that returns just the server_name
    from the current tenant configuration.

    Returns:
        The server name string, or None if no tenant context is set.
    """
    tenant = get_current_tenant()
    return tenant.server_name if tenant else None


def get_current_database_schema() -> str | None:
    """Get the database schema for the current tenant context.

    This is a convenience function that returns just the database_schema
    from the current tenant configuration.

    Returns:
        The database schema string, or None if no tenant context is set.
    """
    tenant = get_current_tenant()
    return tenant.database_schema if tenant else None


def is_multi_tenant_context() -> bool:
    """Check if we're currently in a multi-tenant context.

    Returns:
        True if a tenant context is set, False otherwise.
    """
    return _current_tenant.get() is not None


def get_effective_server_notices_mxid(
    global_mxid: str | None,
) -> str | None:
    """Return the server_notices MXID for the current tenant, or the global fallback."""
    tenant = get_current_tenant()
    if tenant:
        return tenant.effective_server_notices_mxid
    return global_mxid


class TenantContextMiddleware:
    """Middleware helper for managing tenant context in request handlers.

    This class provides methods for extracting tenant information from
    HTTP requests and managing the tenant context lifecycle.
    """

    def __init__(self, tenant_registry: "TenantRegistry") -> None:
        """Initialize the middleware.

        Args:
            tenant_registry: The registry to use for tenant lookups.
        """
        from synapse.tenant_registry import TenantRegistry

        self._registry: TenantRegistry = tenant_registry

    def extract_tenant_from_host(self, host: str) -> "TenantConfig | None":
        """Extract the tenant configuration from a Host header value.

        Args:
            host: The Host header value (may include port).

        Returns:
            The TenantConfig if found, None otherwise.
        """
        # Strip port if present
        if ":" in host and not host.startswith("["):
            # Not an IPv6 address, strip port
            host = host.rsplit(":", 1)[0]
        elif host.startswith("[") and "]:" in host:
            # IPv6 address with port
            host = host.rsplit(":", 1)[0]

        return self._registry.get_tenant(host)

    def get_tenant_from_request(self, request: "SynapseRequest") -> "TenantConfig | None":
        """Get the tenant configuration from an HTTP request.

        Extracts the tenant from the Host header or X-Matrix-Server-Name header.

        Args:
            request: The Synapse HTTP request.

        Returns:
            The TenantConfig if found, None otherwise.
        """
        from synapse.http.site import SynapseRequest

        # First try X-Matrix-Server-Name header (allows explicit tenant specification)
        server_name_header = request.getHeader(b"X-Matrix-Server-Name")
        if server_name_header:
            server_name = server_name_header.decode("utf-8", errors="replace")
            tenant = self._registry.get_tenant(server_name)
            if tenant:
                return tenant

        # Fall back to Host header
        host_header = request.getHeader(b"Host")
        if host_header:
            host = host_header.decode("utf-8", errors="replace")
            return self.extract_tenant_from_host(host)

        return None


# Type stubs for imports that may not be available at import time
if TYPE_CHECKING:
    from synapse.http.site import SynapseRequest
    from synapse.tenant_registry import TenantRegistry
