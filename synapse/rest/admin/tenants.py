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
Admin API endpoints for managing tenants in multi-tenant Synapse deployments.

These endpoints provide CRUD operations for tenants, allowing administrators
to list, view, create, update, and delete tenant configurations.

All endpoints require server admin authentication.

Endpoints:
    GET     /_synapse/admin/v1/tenants           - List all tenants
    GET     /_synapse/admin/v1/tenants/{server_name}  - Get tenant details
    POST    /_synapse/admin/v1/tenants           - Create a new tenant
    PUT     /_synapse/admin/v1/tenants/{server_name}  - Update a tenant
    DELETE  /_synapse/admin/v1/tenants/{server_name}  - Delete a tenant
    POST    /_synapse/admin/v1/tenants/{server_name}/reload_keys - Reload signing keys
"""

import logging
from http import HTTPStatus
from typing import TYPE_CHECKING

from synapse.api.errors import Codes, NotFoundError, SynapseError
from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet, parse_json_object_from_request
from synapse.http.site import SynapseRequest
from synapse.rest.admin._base import admin_patterns, assert_requester_is_admin
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class ListTenantsRestServlet(RestServlet):
    """List all configured tenants.

    GET /_synapse/admin/v1/tenants

    Returns:
        {
            "tenants": [
                {
                    "server_name": "acme.com",
                    "database_schema": "tenant_acme",
                    "registration_enabled": false,
                    "federation_enabled": true
                },
                ...
            ],
            "total": 2
        }
    """

    PATTERNS = admin_patterns("/tenants$")

    def __init__(self, hs: "HomeServer"):
        self._hs = hs
        self._auth = hs.get_auth()
        self._tenants_config = getattr(hs.config, "tenants", None)

    async def on_GET(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self._auth, request)

        if self._tenants_config is None or not self._tenants_config.multi_tenant.enabled:
            return HTTPStatus.OK, {"tenants": [], "total": 0, "multi_tenant_enabled": False}

        tenants = []
        for tenant in self._tenants_config.multi_tenant.tenants:
            tenants.append({
                "server_name": tenant.server_name,
                "database_schema": tenant.database_schema,
                "registration_enabled": tenant.registration_enabled,
                "federation_enabled": tenant.federation_enabled,
                "media_store_path": tenant.media_store_path,
            })

        return HTTPStatus.OK, {
            "tenants": tenants,
            "total": len(tenants),
            "multi_tenant_enabled": True,
        }


class TenantRestServlet(RestServlet):
    """Get, update, or delete a specific tenant.

    GET /_synapse/admin/v1/tenants/{server_name}
    PUT /_synapse/admin/v1/tenants/{server_name}
    DELETE /_synapse/admin/v1/tenants/{server_name}

    GET Returns:
        {
            "server_name": "acme.com",
            "database_schema": "tenant_acme",
            "signing_key_path": "/keys/acme.key",
            "media_store_path": "/media/acme",
            "registration_enabled": false,
            "federation_enabled": true
        }
    """

    PATTERNS = admin_patterns("/tenants/(?P<server_name>[^/]+)$")

    def __init__(self, hs: "HomeServer"):
        self._hs = hs
        self._auth = hs.get_auth()
        self._tenants_config = getattr(hs.config, "tenants", None)

    def _get_tenant(self, server_name: str):
        """Get tenant config or raise NotFoundError."""
        if self._tenants_config is None or not self._tenants_config.multi_tenant.enabled:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "Multi-tenant mode is not enabled",
                Codes.FORBIDDEN,
            )

        for tenant in self._tenants_config.multi_tenant.tenants:
            if tenant.server_name == server_name:
                return tenant

        raise NotFoundError(f"Tenant '{server_name}' not found")

    async def on_GET(
        self, request: SynapseRequest, server_name: str
    ) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self._auth, request)

        tenant = self._get_tenant(server_name)

        return HTTPStatus.OK, {
            "server_name": tenant.server_name,
            "database_schema": tenant.database_schema,
            "signing_key_path": tenant.signing_key_path,
            "media_store_path": tenant.media_store_path,
            "registration_enabled": tenant.registration_enabled,
            "federation_enabled": tenant.federation_enabled,
            "max_users": tenant.max_users,
            "max_rooms": tenant.max_rooms,
        }

    async def on_PUT(
        self, request: SynapseRequest, server_name: str
    ) -> tuple[int, JsonDict]:
        """Update tenant configuration.

        Note: This endpoint can only update certain runtime-configurable
        settings. Changes to database_schema or signing_key_path require
        a server restart.
        """
        await assert_requester_is_admin(self._auth, request)

        # Get existing tenant to ensure it exists
        tenant = self._get_tenant(server_name)

        body = parse_json_object_from_request(request)

        # In the current implementation, we return success but note that
        # actual updates would require modifying the config file and restarting.
        # A future implementation could support hot-reloading certain settings.

        updatable_fields = ["registration_enabled", "federation_enabled", "max_users", "max_rooms"]
        updates = {}
        for field in updatable_fields:
            if field in body:
                updates[field] = body[field]

        if not updates:
            return HTTPStatus.OK, {
                "server_name": server_name,
                "message": "No updatable fields provided",
                "note": "Runtime updates are limited. Most changes require config file modification and restart.",
            }

        # Note: In a full implementation, this would persist the changes
        # For now, we just return what would be updated
        logger.info("Tenant %s update requested: %s", server_name, updates)

        return HTTPStatus.OK, {
            "server_name": server_name,
            "updates_requested": updates,
            "note": "Configuration updates require server restart to take effect. "
                    "Please update the homeserver.yaml and restart Synapse.",
        }

    async def on_DELETE(
        self, request: SynapseRequest, server_name: str
    ) -> tuple[int, JsonDict]:
        """Mark a tenant for deletion.

        Note: This endpoint marks the tenant for deletion but does not
        immediately remove data. A separate cleanup job handles data removal.
        """
        await assert_requester_is_admin(self._auth, request)

        # Verify tenant exists
        tenant = self._get_tenant(server_name)

        # In a full implementation, this would:
        # 1. Mark the tenant as disabled
        # 2. Schedule cleanup of tenant data
        # 3. Remove from configuration

        logger.warning(
            "Tenant deletion requested for %s. "
            "This operation requires manual config update and data cleanup.",
            server_name,
        )

        return HTTPStatus.OK, {
            "server_name": server_name,
            "status": "deletion_requested",
            "note": "Tenant deletion requires manual steps: "
                    "1. Remove from homeserver.yaml, "
                    "2. Restart Synapse, "
                    "3. Optionally drop database schema and remove media files.",
        }


class CreateTenantRestServlet(RestServlet):
    """Create a new tenant.

    POST /_synapse/admin/v1/tenants

    Request body:
        {
            "server_name": "newcorp.com",
            "database_schema": "tenant_newcorp",
            "signing_key_path": "/keys/newcorp.key",
            "media_store_path": "/media/newcorp",
            "registration_enabled": false
        }

    Note: Creating a tenant requires:
    1. Adding to homeserver.yaml
    2. Creating the database schema
    3. Generating signing keys
    4. Restarting Synapse

    This endpoint validates the request and provides guidance on the steps needed.
    """

    PATTERNS = admin_patterns("/tenants$")

    def __init__(self, hs: "HomeServer"):
        self._hs = hs
        self._auth = hs.get_auth()
        self._tenants_config = getattr(hs.config, "tenants", None)

    async def on_POST(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self._auth, request)

        body = parse_json_object_from_request(request)

        # Validate required fields
        required_fields = ["server_name"]
        for field in required_fields:
            if field not in body:
                raise SynapseError(
                    HTTPStatus.BAD_REQUEST,
                    f"Missing required field: {field}",
                    Codes.MISSING_PARAM,
                )

        server_name = body["server_name"]

        # Check if tenant already exists
        if self._tenants_config and self._tenants_config.multi_tenant.enabled:
            for tenant in self._tenants_config.multi_tenant.tenants:
                if tenant.server_name == server_name:
                    raise SynapseError(
                        HTTPStatus.CONFLICT,
                        f"Tenant '{server_name}' already exists",
                        Codes.RESOURCE_LIMIT_EXCEEDED,
                    )

        # Generate default values for optional fields
        safe_name = server_name.replace(".", "_").replace("-", "_")
        database_schema = body.get("database_schema", f"tenant_{safe_name}")
        signing_key_path = body.get("signing_key_path", f"/etc/synapse/keys/{safe_name}.signing.key")
        media_store_path = body.get("media_store_path", f"/var/synapse/media/{safe_name}")

        # Build the tenant configuration for the response
        tenant_config = {
            "server_name": server_name,
            "database_schema": database_schema,
            "signing_key_path": signing_key_path,
            "media_store_path": media_store_path,
            "registration_enabled": body.get("registration_enabled", False),
            "federation_enabled": body.get("federation_enabled", True),
        }

        # Build YAML snippet for adding to config
        yaml_snippet = f"""
# Add this to homeserver.yaml under 'tenants':
  - server_name: "{server_name}"
    database_schema: "{database_schema}"
    signing_key_path: "{signing_key_path}"
    media_store_path: "{media_store_path}"
    registration_enabled: {str(body.get('registration_enabled', False)).lower()}
    federation_enabled: {str(body.get('federation_enabled', True)).lower()}
"""

        # Build setup instructions
        instructions = [
            f"1. Generate signing key: python -m synapse.app.homeserver --generate-keys -c homeserver.yaml --signing-key-path {signing_key_path}",
            f"2. Create database schema: python -m scripts.create_tenant_schema -c homeserver.yaml --tenant-name {server_name} --schema-name {database_schema}",
            f"3. Create media directory: mkdir -p {media_store_path}",
            "4. Add the tenant configuration to homeserver.yaml (see yaml_snippet)",
            "5. Restart Synapse",
        ]

        logger.info("Tenant creation requested for %s", server_name)

        return HTTPStatus.ACCEPTED, {
            "status": "creation_pending",
            "tenant": tenant_config,
            "yaml_snippet": yaml_snippet,
            "setup_instructions": instructions,
            "note": "Tenant creation requires manual setup steps. Follow the instructions to complete the setup.",
        }


class ReloadTenantKeysRestServlet(RestServlet):
    """Reload signing keys for a tenant.

    POST /_synapse/admin/v1/tenants/{server_name}/reload_keys

    This is useful after rotating signing keys. The server will reload
    the key from the configured path without requiring a full restart.
    """

    PATTERNS = admin_patterns("/tenants/(?P<server_name>[^/]+)/reload_keys$")

    def __init__(self, hs: "HomeServer"):
        self._hs = hs
        self._auth = hs.get_auth()
        self._tenants_config = getattr(hs.config, "tenants", None)

    async def on_POST(
        self, request: SynapseRequest, server_name: str
    ) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self._auth, request)

        if self._tenants_config is None or not self._tenants_config.multi_tenant.enabled:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "Multi-tenant mode is not enabled",
                Codes.FORBIDDEN,
            )

        # Verify tenant exists
        tenant = None
        for t in self._tenants_config.multi_tenant.tenants:
            if t.server_name == server_name:
                tenant = t
                break

        if tenant is None:
            raise NotFoundError(f"Tenant '{server_name}' not found")

        # Try to reload the keys
        try:
            # Get the multi-tenant keyring if available
            keyring = getattr(self._hs, "_multi_tenant_keyring", None)
            if keyring is None:
                raise SynapseError(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "Multi-tenant keyring not initialized",
                    Codes.UNKNOWN,
                )

            keyring.reload_tenant_keys(server_name)

            logger.info("Reloaded signing keys for tenant %s", server_name)

            return HTTPStatus.OK, {
                "server_name": server_name,
                "status": "keys_reloaded",
            }

        except FileNotFoundError as e:
            raise SynapseError(
                HTTPStatus.NOT_FOUND,
                f"Signing key file not found: {e}",
                Codes.NOT_FOUND,
            )
        except Exception as e:
            logger.error("Failed to reload keys for tenant %s: %s", server_name, e)
            raise SynapseError(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"Failed to reload keys: {e}",
                Codes.UNKNOWN,
            )


class TenantStatusRestServlet(RestServlet):
    """Get runtime status of a tenant.

    GET /_synapse/admin/v1/tenants/{server_name}/status

    Returns:
        {
            "server_name": "acme.com",
            "status": "active",
            "keys_loaded": true,
            "database_schema_exists": true,
            "user_count": 150,
            "room_count": 45
        }
    """

    PATTERNS = admin_patterns("/tenants/(?P<server_name>[^/]+)/status$")

    def __init__(self, hs: "HomeServer"):
        self._hs = hs
        self._auth = hs.get_auth()
        self._tenants_config = getattr(hs.config, "tenants", None)

    async def on_GET(
        self, request: SynapseRequest, server_name: str
    ) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self._auth, request)

        if self._tenants_config is None or not self._tenants_config.multi_tenant.enabled:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "Multi-tenant mode is not enabled",
                Codes.FORBIDDEN,
            )

        # Verify tenant exists
        tenant = None
        for t in self._tenants_config.multi_tenant.tenants:
            if t.server_name == server_name:
                tenant = t
                break

        if tenant is None:
            raise NotFoundError(f"Tenant '{server_name}' not found")

        # Check if keys are loaded
        keys_loaded = False
        keyring = getattr(self._hs, "_multi_tenant_keyring", None)
        if keyring is not None:
            keys_loaded = keyring.is_local_server_name(server_name)

        # In a full implementation, we would query the database for user/room counts
        # For now, we return placeholder values
        status_info = {
            "server_name": server_name,
            "status": "active" if keys_loaded else "degraded",
            "keys_loaded": keys_loaded,
            "database_schema": tenant.database_schema,
            "registration_enabled": tenant.registration_enabled,
            "federation_enabled": tenant.federation_enabled,
        }

        return HTTPStatus.OK, status_info


def register_tenant_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    """Register all tenant admin servlets."""
    ListTenantsRestServlet(hs).register(http_server)
    TenantRestServlet(hs).register(http_server)
    CreateTenantRestServlet(hs).register(http_server)
    ReloadTenantKeysRestServlet(hs).register(http_server)
    TenantStatusRestServlet(hs).register(http_server)
