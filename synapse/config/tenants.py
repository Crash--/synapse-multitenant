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
Multi-tenant configuration for Synapse.

This module provides configuration classes for multi-tenant deployments,
allowing a single Synapse process to serve multiple Matrix domains.
"""

import logging
from typing import Any

import attr

from synapse.config._base import Config, ConfigError
from synapse.types import JsonDict

logger = logging.getLogger(__name__)


@attr.s(auto_attribs=True, slots=True, frozen=True)
class TenantConfig:
    """Configuration for a single tenant in a multi-tenant deployment.

    Attributes:
        server_name: The Matrix server name for this tenant (e.g., "acme.com")
        database_schema: The PostgreSQL schema to use for this tenant's data
        signing_key_path: Path to the signing key file for this tenant
        media_store_path: Path to the media storage directory for this tenant
        registration_enabled: Whether new user registration is enabled
        registration_shared_secret: Optional shared secret for registration
        macaroon_secret_key: Secret key for generating macaroons
        form_secret: Secret for signing forms
        enable_federation: Whether federation is enabled for this tenant
        trusted_key_servers: List of trusted key servers for this tenant
        max_mau_value: Maximum monthly active users (0 for unlimited)
    """

    server_name: str
    database_schema: str
    signing_key_path: str
    media_store_path: str
    registration_enabled: bool = False
    registration_shared_secret: str | None = None
    macaroon_secret_key: str | None = None
    form_secret: str | None = None
    enable_federation: bool = True
    trusted_key_servers: list[str] = attr.Factory(list)
    max_mau_value: int = 0
    # Public base URL announced in /.well-known/matrix/client and used to
    # build links in outgoing emails (password reset, registration
    # confirmation) and in OIDC callbacks. If unset, defaults to
    # `https://{server_name}/` — the conventional Matrix discovery URL.
    public_baseurl: str | None = None

    @property
    def effective_public_baseurl(self) -> str:
        """Return `public_baseurl` if set, otherwise the conventional default.

        The default matches what upstream Synapse derives from `server_name`
        when `public_baseurl` is unset, so per-tenant behaviour stays
        aligned with vanilla Synapse defaults.
        """
        if self.public_baseurl is not None:
            return self.public_baseurl
        return f"https://{self.server_name}/"

    @classmethod
    def from_dict(cls, config: JsonDict, base_path: str = "") -> "TenantConfig":
        """Create a TenantConfig from a configuration dictionary.

        Args:
            config: Dictionary containing tenant configuration
            base_path: Base path for resolving relative paths

        Returns:
            A TenantConfig instance

        Raises:
            ConfigError: If required configuration is missing or invalid
        """
        server_name = config.get("server_name")
        if not server_name:
            raise ConfigError("Tenant configuration missing 'server_name'")

        # Default schema name based on server_name (sanitized)
        default_schema = "tenant_" + server_name.replace(".", "_").replace("-", "_")
        database_schema = config.get("database_schema", default_schema)

        signing_key_path = config.get("signing_key_path")
        if not signing_key_path:
            raise ConfigError(
                f"Tenant '{server_name}' missing 'signing_key_path'"
            )

        # Default media path based on server_name
        default_media_path = f"{base_path}/media_store/{server_name}"
        media_store_path = config.get("media_store_path", default_media_path)

        return cls(
            server_name=server_name,
            database_schema=database_schema,
            signing_key_path=signing_key_path,
            media_store_path=media_store_path,
            registration_enabled=config.get("registration_enabled", False),
            registration_shared_secret=config.get("registration_shared_secret"),
            macaroon_secret_key=config.get("macaroon_secret_key"),
            form_secret=config.get("form_secret"),
            enable_federation=config.get("enable_federation", True),
            trusted_key_servers=config.get("trusted_key_servers", []),
            max_mau_value=config.get("max_mau_value", 0),
            public_baseurl=config.get("public_baseurl"),
        )


@attr.s(auto_attribs=True, slots=True)
class MultiTenantConfig:
    """Configuration for multi-tenant mode.

    Attributes:
        enabled: Whether multi-tenant mode is enabled
        default_schema: Default database schema for shared data
        tenants: Dictionary mapping server_name to TenantConfig
    """

    enabled: bool = False
    default_schema: str = "public"
    tenants: dict[str, TenantConfig] = attr.Factory(dict)

    def get_tenant(self, server_name: str) -> TenantConfig | None:
        """Get the tenant configuration for a given server name.

        Args:
            server_name: The Matrix server name to look up

        Returns:
            The TenantConfig if found, None otherwise
        """
        return self.tenants.get(server_name)

    def get_all_server_names(self) -> list[str]:
        """Get all configured server names.

        Returns:
            List of all tenant server names
        """
        return list(self.tenants.keys())


class TenantsConfig(Config):
    """Synapse configuration section for multi-tenant settings."""

    section = "tenants"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        """Read the tenants configuration section.

        The configuration format is:

        ```yaml
        multi_tenant:
          enabled: true
          default_schema: "public"

        tenants:
          - server_name: "acme.com"
            database_schema: "tenant_acme"
            signing_key_path: "/keys/acme.key"
            media_store_path: "/media/acme"
            registration_enabled: false

          - server_name: "corp.io"
            database_schema: "tenant_corp"
            signing_key_path: "/keys/corp.key"
        ```
        """
        multi_tenant_config = config.get("multi_tenant", {})

        enabled = multi_tenant_config.get("enabled", False)
        default_schema = multi_tenant_config.get("default_schema", "public")

        tenants_dict: dict[str, TenantConfig] = {}

        if enabled:
            tenants_list = config.get("tenants", [])
            if not tenants_list:
                raise ConfigError(
                    "Multi-tenant mode enabled but no tenants configured"
                )

            # Get base path for resolving relative paths
            base_path = kwargs.get("config_dir_path", "")

            for tenant_config in tenants_list:
                try:
                    tenant = TenantConfig.from_dict(tenant_config, base_path)
                    if tenant.server_name in tenants_dict:
                        raise ConfigError(
                            f"Duplicate tenant server_name: {tenant.server_name}"
                        )
                    tenants_dict[tenant.server_name] = tenant
                    logger.info(
                        "Loaded tenant configuration for %s (schema: %s)",
                        tenant.server_name,
                        tenant.database_schema,
                    )
                except Exception as e:
                    raise ConfigError(
                        f"Error loading tenant configuration: {e}"
                    ) from e

        self.multi_tenant = MultiTenantConfig(
            enabled=enabled,
            default_schema=default_schema,
            tenants=tenants_dict,
        )

    def generate_config_section(self, **kwargs: Any) -> str:
        """Generate a sample configuration section."""
        return """\
        ## Multi-Tenant Configuration ##
        # Enable multi-tenant mode to serve multiple Matrix domains from a single
        # Synapse process. Each tenant has isolated data using PostgreSQL schemas.
        #
        #multi_tenant:
        #  enabled: true
        #  default_schema: "public"
        #
        #tenants:
        #  - server_name: "acme.com"
        #    database_schema: "tenant_acme"
        #    signing_key_path: "/etc/synapse/keys/acme.signing.key"
        #    media_store_path: "/var/lib/synapse/media/acme"
        #    registration_enabled: false
        #    max_mau_value: 1000
        #
        #  - server_name: "corp.io"
        #    database_schema: "tenant_corp"
        #    signing_key_path: "/etc/synapse/keys/corp.signing.key"
        #    media_store_path: "/var/lib/synapse/media/corp"
        #    registration_enabled: true
        #    registration_shared_secret: "CHANGEME"
        """
