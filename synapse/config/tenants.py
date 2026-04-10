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
class TenantEmailConfig:
    """Per-tenant email/SMTP configuration.

    When present on a TenantConfig, fully replaces the global email
    config for that tenant (no field-level merge).
    """

    notif_from: str
    smtp_host: str = "localhost"
    smtp_port: int = 25  # overridden to 465 by from_dict when force_tls
    smtp_user: str | None = None
    smtp_pass: str | None = None
    require_transport_security: bool = False
    enable_tls: bool = True
    force_tls: bool = False
    tlsname: str | None = None
    app_name: str = "Matrix"
    riot_base_url: str | None = None
    notif_delay_before_mail_ms: int = 300_000  # 5 minutes default

    @classmethod
    def from_dict(cls, d: "JsonDict") -> "TenantEmailConfig":
        force_tls = d.get("force_tls", False)
        default_port = 465 if force_tls else 25
        return cls(
            notif_from=d["notif_from"],
            smtp_host=d.get("smtp_host", "localhost"),
            smtp_port=d.get("smtp_port", default_port),
            smtp_user=d.get("smtp_user"),
            smtp_pass=d.get("smtp_pass"),
            require_transport_security=d.get("require_transport_security", False),
            enable_tls=d.get("enable_tls", True),
            force_tls=force_tls,
            tlsname=d.get("tlsname"),
            app_name=d.get("app_name", "Matrix"),
            riot_base_url=d.get("riot_base_url"),
            notif_delay_before_mail_ms=d.get(
                "notif_delay_before_mail_ms", 300_000
            ),
        )


@attr.s(auto_attribs=True, slots=True, frozen=True)
class TenantOidcConfig:
    """Per-tenant OIDC configuration.
    Stores raw provider dicts that get parsed by _parse_oidc_provider_configs
    at handler init time."""
    providers: tuple = attr.Factory(tuple)

    @classmethod
    def from_list(cls, providers: list) -> "TenantOidcConfig":
        return cls(providers=tuple(providers))


@attr.s(auto_attribs=True, slots=True, frozen=True)
class TenantCasConfig:
    """Per-tenant CAS configuration."""
    server_url: str
    protocol_version: int | None = None
    displayname_attribute: str | None = None
    required_attributes: dict = attr.Factory(dict)
    enable_registration: bool = True
    allow_numeric_ids: bool = False
    numeric_ids_prefix: str = "u"
    idp_name: str = "CAS"
    idp_icon: str | None = None
    idp_brand: str | None = None

    @classmethod
    def from_dict(cls, d: "JsonDict") -> "TenantCasConfig":
        return cls(
            server_url=d["server_url"],
            protocol_version=d.get("protocol_version"),
            displayname_attribute=d.get("displayname_attribute"),
            required_attributes=d.get("required_attributes", {}),
            enable_registration=d.get("enable_registration", True),
            allow_numeric_ids=d.get("allow_numeric_ids", False),
            numeric_ids_prefix=d.get("numeric_ids_prefix", "u"),
            idp_name=d.get("idp_name", "CAS"),
            idp_icon=d.get("idp_icon"),
            idp_brand=d.get("idp_brand"),
        )


@attr.s(auto_attribs=True, slots=True, frozen=True)
class TenantSamlConfig:
    """Per-tenant SAML configuration. Minimal: stores IdP entity ID and session lifetime.
    Full Saml2Config SP construction is deferred to handler init."""
    idp_entityid: str | None = None
    session_lifetime: str = "15m"
    raw_config: dict = attr.Factory(dict)

    @classmethod
    def from_dict(cls, d: "JsonDict") -> "TenantSamlConfig":
        return cls(
            idp_entityid=d.get("idp_entityid"),
            session_lifetime=d.get("session_lifetime", "15m"),
            raw_config=dict(d),
        )


@attr.s(auto_attribs=True, slots=True, frozen=True)
class TenantPushConfig:
    """Per-tenant push notification configuration."""
    include_content: bool = True
    enabled: bool = True
    group_unread_count_by_room: bool = True
    jitter_delay_ms: int | None = None

    @classmethod
    def from_dict(cls, d: "JsonDict") -> "TenantPushConfig":
        return cls(
            include_content=d.get("include_content", True),
            enabled=d.get("enabled", True),
            group_unread_count_by_room=d.get("group_unread_count_by_room", True),
            jitter_delay_ms=d.get("jitter_delay_ms"),
        )


@attr.s(auto_attribs=True, slots=True, frozen=True)
class TenantRatelimitConfig:
    """Per-tenant rate-limit settings.

    Each field is a RatelimitSettings or None. None means "inherit
    the global RatelimitConfig value for this limiter".
    """

    rc_message: "RatelimitSettings | None" = None
    rc_registration: "RatelimitSettings | None" = None
    rc_registration_token_validity: "RatelimitSettings | None" = None
    rc_login_address: "RatelimitSettings | None" = None
    rc_login_account: "RatelimitSettings | None" = None
    rc_login_failed_attempts: "RatelimitSettings | None" = None
    rc_joins_local: "RatelimitSettings | None" = None
    rc_joins_remote: "RatelimitSettings | None" = None
    rc_joins_per_room: "RatelimitSettings | None" = None
    rc_invites_per_room: "RatelimitSettings | None" = None
    rc_invites_per_user: "RatelimitSettings | None" = None
    rc_invites_per_issuer: "RatelimitSettings | None" = None
    rc_third_party_invite: "RatelimitSettings | None" = None
    rc_3pid_validation: "RatelimitSettings | None" = None
    rc_media_create: "RatelimitSettings | None" = None
    rc_presence_per_user: "RatelimitSettings | None" = None
    rc_user_directory: "RatelimitSettings | None" = None

    @classmethod
    def from_dict(cls, d: "JsonDict") -> "TenantRatelimitConfig":
        from synapse.config.ratelimiting import RatelimitSettings

        def _parse(key: str) -> "RatelimitSettings | None":
            rl_config = d
            for part in key.split("."):
                if not isinstance(rl_config, dict):
                    return None
                rl_config = rl_config.get(part)
                if rl_config is None:
                    return None
            if not isinstance(rl_config, dict):
                return None
            return RatelimitSettings(
                key=key,
                per_second=float(rl_config.get("per_second", 0.17)),
                burst_count=int(rl_config.get("burst_count", 3)),
            )

        return cls(
            rc_message=_parse("rc_message"),
            rc_registration=_parse("rc_registration"),
            rc_registration_token_validity=_parse("rc_registration_token_validity"),
            rc_login_address=_parse("rc_login.address"),
            rc_login_account=_parse("rc_login.account"),
            rc_login_failed_attempts=_parse("rc_login.failed_attempts"),
            rc_joins_local=_parse("rc_joins.local"),
            rc_joins_remote=_parse("rc_joins.remote"),
            rc_joins_per_room=_parse("rc_joins_per_room"),
            rc_invites_per_room=_parse("rc_invites.per_room"),
            rc_invites_per_user=_parse("rc_invites.per_user"),
            rc_invites_per_issuer=_parse("rc_invites.per_issuer"),
            rc_third_party_invite=_parse("rc_third_party_invite"),
            rc_3pid_validation=_parse("rc_3pid_validation"),
            rc_media_create=_parse("rc_media_create"),
            rc_presence_per_user=_parse("rc_presence.per_user"),
            rc_user_directory=_parse("rc_user_directory"),
        )


@attr.s(auto_attribs=True, slots=True, frozen=True)
class TenantConfig:
    """Configuration for a single tenant in a multi-tenant deployment.

    Attributes:
        server_name: The Matrix server name for this tenant (e.g., "acme.com")
        database_schema: The PostgreSQL schema to use for this tenant's data
        signing_key_path: Path to the signing key file for this tenant (None for DB-sourced keys)
        media_store_path: Path to the media storage directory for this tenant
        registration_enabled: Whether new user registration is enabled
        registration_shared_secret: Optional shared secret for registration
        macaroon_secret_key: Secret key for generating macaroons
        form_secret: Secret for signing forms
        enable_federation: Whether federation is enabled for this tenant
        trusted_key_servers: List of trusted key servers for this tenant
        max_mau_value: Maximum monthly active users (0 for unlimited)
        signing_key_data: In-memory signing key material (when loaded from DB
            rather than filesystem). Mutually exclusive with signing_key_path.
    """

    server_name: str
    database_schema: str
    signing_key_path: str | None
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
    # Identity server URL announced in /.well-known/matrix/client under
    # the `m.identity_server` key. If unset, the tenant inherits whatever
    # the global config has (which may also be unset, meaning no
    # identity server is announced).
    identity_server: str | None = None
    # Per-tenant server-notices sender MXID (e.g.
    # "@notices:acme.localhost"). Under multi-tenant the upstream global
    # `server_notices.server_notices_mxid` is wrong because it bakes the
    # primary hostname into every notice's `sender` field -- a stored-row
    # corruption where notices sent on corp.localhost carried
    # `@notices:acme.localhost` as their author. If unset, the tenant
    # falls back to the conventional `@notices:{server_name}` form so
    # MXIDs always live on the sending tenant's domain.
    server_notices_mxid: str | None = None
    # Per-tenant email/SMTP configuration. When set, fully replaces the
    # global email config for this tenant (no field-level merge). When
    # None, the tenant inherits the global email config unchanged.
    email: TenantEmailConfig | None = None
    # Per-tenant SSO configuration. When set, configures the respective
    # SSO provider for this tenant. When None, the tenant inherits the
    # global SSO config (or has no SSO if not configured globally).
    oidc: TenantOidcConfig | None = None
    cas: TenantCasConfig | None = None
    saml: TenantSamlConfig | None = None
    push: TenantPushConfig | None = None
    # Per-tenant rate-limit overrides. When set, individual limiters
    # override the corresponding global rc_* setting. When None, the
    # tenant inherits all global rate limits unchanged.
    ratelimit: TenantRatelimitConfig | None = None
    # Per-tenant app service config file paths. When set, only these
    # AS registrations apply to this tenant. When None, no app services
    # are active for this tenant.
    app_service_config_files: list[str] | None = None
    # In-memory signing key data (for DB-sourced tenants). When set,
    # signing_key_path should be None. The keyring reads from this
    # instead of the filesystem.
    signing_key_data: str | None = None

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

    @property
    def effective_server_notices_mxid(self) -> str:
        """Return the MXID server notices should be sent from for this tenant.

        Falls back to `@notices:{server_name}` when `server_notices_mxid`
        is unset so the MXID always lives on the tenant's own domain.
        Callers that need to know whether server notices are *enabled*
        for a tenant should check the global `server_notices.enabled`
        config -- this accessor only resolves the MXID.
        """
        if self.server_notices_mxid is not None:
            return self.server_notices_mxid
        return f"@notices:{self.server_name}"

    @property
    def effective_identity_server(self) -> str | None:
        """Return `identity_server` if set, otherwise None.

        Unlike `public_baseurl`, there is no default — the identity
        server is genuinely optional and the wire format omits the
        `m.identity_server` key entirely when unset.
        """
        return self.identity_server

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

        email_dict = config.get("email")
        email_cfg = TenantEmailConfig.from_dict(email_dict) if email_dict else None

        oidc_list = config.get("oidc_providers")
        oidc_cfg = TenantOidcConfig.from_list(oidc_list) if oidc_list else None

        cas_dict = config.get("cas")
        cas_cfg = TenantCasConfig.from_dict(cas_dict) if cas_dict else None

        saml_dict = config.get("saml")
        saml_cfg = TenantSamlConfig.from_dict(saml_dict) if saml_dict else None

        push_dict = config.get("push")
        push_cfg = TenantPushConfig.from_dict(push_dict) if push_dict is not None else None

        ratelimit_dict = config.get("ratelimit")
        ratelimit_cfg = (
            TenantRatelimitConfig.from_dict(ratelimit_dict)
            if ratelimit_dict
            else None
        )
        as_config_files = config.get("app_service_config_files")

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
            identity_server=config.get("identity_server"),
            server_notices_mxid=config.get("server_notices_mxid"),
            email=email_cfg,
            oidc=oidc_cfg,
            cas=cas_cfg,
            saml=saml_cfg,
            push=push_cfg,
            ratelimit=ratelimit_cfg,
            app_service_config_files=as_config_files,
        )

    @classmethod
    def from_db_row(cls, row: JsonDict) -> "TenantConfig":
        """Create a TenantConfig from a database row.

        Similar to ``from_dict`` but expects columns from the
        ``public.tenants`` table. Signing key material comes from the
        ``signing_key_data`` field (already decrypted) rather than a
        filesystem path.

        Args:
            row: Dictionary of column values from the tenants table.

        Returns:
            A TenantConfig instance with ``signing_key_path=None`` and
            ``signing_key_data`` populated.

        Raises:
            ConfigError: If required columns are missing.
        """
        server_name = row.get("server_name")
        if not server_name:
            raise ConfigError("Tenant DB row missing 'server_name'")

        database_schema = row.get("database_schema")
        if not database_schema:
            raise ConfigError(
                f"Tenant '{server_name}' DB row missing 'database_schema'"
            )

        media_store_path = row.get("media_store_path")
        if not media_store_path:
            raise ConfigError(
                f"Tenant '{server_name}' DB row missing 'media_store_path'"
            )

        signing_key_data = row.get("signing_key_data")
        if not signing_key_data:
            raise ConfigError(
                f"Tenant '{server_name}' DB row missing 'signing_key_data'"
            )

        # Parse JSONB sub-config columns through existing from_dict() methods
        email_raw = row.get("email_config")
        email_cfg = TenantEmailConfig.from_dict(email_raw) if email_raw else None

        oidc_raw = row.get("oidc_config")
        oidc_cfg = TenantOidcConfig.from_list(oidc_raw) if oidc_raw else None

        cas_raw = row.get("cas_config")
        cas_cfg = TenantCasConfig.from_dict(cas_raw) if cas_raw else None

        saml_raw = row.get("saml_config")
        saml_cfg = TenantSamlConfig.from_dict(saml_raw) if saml_raw else None

        push_raw = row.get("push_config")
        push_cfg = TenantPushConfig.from_dict(push_raw) if push_raw else None

        ratelimit_raw = row.get("ratelimit_config")
        ratelimit_cfg = (
            TenantRatelimitConfig.from_dict(ratelimit_raw)
            if ratelimit_raw
            else None
        )

        return cls(
            server_name=server_name,
            database_schema=database_schema,
            signing_key_path=None,
            media_store_path=media_store_path,
            registration_enabled=row.get("registration_enabled", False),
            registration_shared_secret=row.get("registration_shared_secret"),
            macaroon_secret_key=row.get("macaroon_secret_key"),
            form_secret=row.get("form_secret"),
            enable_federation=row.get("enable_federation", True),
            trusted_key_servers=row.get("trusted_key_servers", []),
            max_mau_value=row.get("max_mau_value", 0),
            public_baseurl=row.get("public_baseurl"),
            identity_server=row.get("identity_server"),
            server_notices_mxid=row.get("server_notices_mxid"),
            email=email_cfg,
            oidc=oidc_cfg,
            cas=cas_cfg,
            saml=saml_cfg,
            push=push_cfg,
            ratelimit=ratelimit_cfg,
            app_service_config_files=row.get("app_service_config_files"),
            signing_key_data=signing_key_data,
        )


@attr.s(auto_attribs=True, slots=True)
class MultiTenantConfig:
    """Configuration for multi-tenant mode.

    Attributes:
        enabled: Whether multi-tenant mode is enabled
        default_schema: Default database schema for shared data
        source: Where tenant definitions come from — ``"yaml"`` (default,
            parsed from ``homeserver.yaml``) or ``"database"`` (loaded from
            the ``public.tenants`` table at startup and on reload).
        reload_secret: Shared secret for authenticating reload requests
            from the control plane (``POST /_synapse/admin/v1/tenants/reload``).
        tenants: Dictionary mapping server_name to TenantConfig
    """

    enabled: bool = False
    default_schema: str = "public"
    source: str = "yaml"
    reload_secret: str | None = None
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
        source = multi_tenant_config.get("source", "yaml")
        reload_secret = multi_tenant_config.get("reload_secret")

        if source not in ("yaml", "database"):
            raise ConfigError(
                f"multi_tenant.source must be 'yaml' or 'database', got '{source}'"
            )

        tenants_dict: dict[str, TenantConfig] = {}

        if enabled and source == "yaml":
            # YAML-sourced tenants: parse from homeserver.yaml as before.
            tenants_list = config.get("tenants", [])
            if not tenants_list:
                raise ConfigError(
                    "Multi-tenant mode enabled with source 'yaml' but no tenants configured"
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
        elif enabled and source == "database":
            # Database-sourced tenants: loaded at startup by the registry
            # from public.tenants. Zero tenants is valid — the control
            # plane will add them at runtime.
            logger.info(
                "Multi-tenant mode enabled with source 'database'; "
                "tenants will be loaded from the public.tenants table"
            )

        self.multi_tenant = MultiTenantConfig(
            enabled=enabled,
            default_schema=default_schema,
            source=source,
            reload_secret=reload_secret,
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
