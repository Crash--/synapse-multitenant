#!/usr/bin/env python3
"""
Multi-Tenant Synapse Demo Server (Standalone)

This is a standalone server that demonstrates multi-tenant routing
without requiring the full Synapse codebase.
"""

import json
import os
import sys
import hashlib
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from dataclasses import dataclass
from typing import Dict, Optional
from contextlib import contextmanager
import contextvars

# Try to import signedjson, fall back to simple implementation
try:
    from signedjson.key import read_signing_keys, get_verify_key, generate_signing_key, write_signing_keys
    from signedjson.sign import sign_json
    HAS_SIGNEDJSON = True
except ImportError:
    HAS_SIGNEDJSON = False
    print("Warning: signedjson not available, using simple signing")


# ============================================================================
# TENANT CONFIGURATION (standalone implementation)
# ============================================================================

@dataclass(frozen=True)
class TenantConfig:
    """Configuration for a single tenant."""
    server_name: str
    database_schema: str
    signing_key_path: str
    media_store_path: str
    registration_enabled: bool = False
    enable_federation: bool = True


# Context variable for current tenant
_current_tenant: contextvars.ContextVar[Optional[TenantConfig]] = contextvars.ContextVar(
    "current_tenant", default=None
)


def get_current_tenant() -> Optional[TenantConfig]:
    return _current_tenant.get()


@contextmanager
def tenant_context(tenant: TenantConfig):
    token = _current_tenant.set(tenant)
    try:
        yield
    finally:
        _current_tenant.reset(token)


# ============================================================================
# CONFIGURATION
# ============================================================================

TENANTS_CONFIG = {
    "acme.localhost": {
        "server_name": "acme.localhost",
        "database_schema": "tenant_acme",
        "signing_key_path": "/keys/acme_localhost.signing.key",
        "media_store_path": "/media/acme",
    },
    "corp.localhost": {
        "server_name": "corp.localhost",
        "database_schema": "tenant_corp",
        "signing_key_path": "/keys/corp_localhost.signing.key",
        "media_store_path": "/media/corp",
    },
    "startup.localhost": {
        "server_name": "startup.localhost",
        "database_schema": "tenant_startup",
        "signing_key_path": "/keys/startup_localhost.signing.key",
        "media_store_path": "/media/startup",
    },
}


class MultiTenantServer:
    """Multi-tenant server state."""

    def __init__(self):
        self.tenants: Dict[str, TenantConfig] = {}
        self.signing_keys: Dict[str, any] = {}
        self._load_tenants()

    def _load_tenants(self):
        """Load all tenant configurations."""
        print("Loading tenant configurations...")

        for name, config in TENANTS_CONFIG.items():
            key_path = config["signing_key_path"]

            # Wait for key file
            for attempt in range(30):
                if os.path.exists(key_path):
                    break
                print(f"  Waiting for {key_path} (attempt {attempt + 1}/30)...")
                time.sleep(1)

            if not os.path.exists(key_path):
                print(f"  ERROR: Key file not found: {key_path}")
                continue

            tenant = TenantConfig(
                server_name=config["server_name"],
                database_schema=config["database_schema"],
                signing_key_path=config["signing_key_path"],
                media_store_path=config["media_store_path"],
            )
            self.tenants[name] = tenant

            # Load signing key
            if HAS_SIGNEDJSON:
                with open(key_path, "r") as f:
                    keys = read_signing_keys(f)
                self.signing_keys[name] = keys[0]
            else:
                with open(key_path, "r") as f:
                    self.signing_keys[name] = f.read().strip()

            print(f"  ✓ Loaded: {name} (schema: {config['database_schema']})")

        print(f"\nServer initialized with {len(self.tenants)} tenants")

    def get_tenant_from_host(self, host: str) -> Optional[TenantConfig]:
        """Get tenant from Host header."""
        if ":" in host:
            host = host.split(":")[0]
        return self.tenants.get(host)

    def sign_response(self, data: dict, server_name: str) -> dict:
        """Sign a response with tenant's key."""
        if server_name not in self.signing_keys:
            return data

        if HAS_SIGNEDJSON:
            key = self.signing_keys[server_name]
            return sign_json(data, server_name, key)
        else:
            # Simple signing fallback
            result = data.copy()
            key = self.signing_keys[server_name]
            message = json.dumps(data, sort_keys=True)
            sig = hashlib.sha256((message + key).encode()).hexdigest()[:32]
            result["signatures"] = {server_name: {"demo:key": sig}}
            return result


# Global server instance
server_state = None


# In-memory storage for demo (simulates database per tenant)
tenant_data: Dict[str, Dict] = {}


def get_tenant_storage(tenant_name: str) -> Dict:
    """Get or create storage for a tenant."""
    if tenant_name not in tenant_data:
        tenant_data[tenant_name] = {
            "users": {},
            "rooms": {},
            "messages": {},
            "access_tokens": {},
        }
    return tenant_data[tenant_name]


class TenantRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler with tenant routing."""

    def log_message(self, format, *args):
        tenant = getattr(self, '_tenant', None)
        tenant_name = tenant.server_name if tenant else "NO_TENANT"
        print(f"[{tenant_name}] {self.requestline}")

    def _get_tenant(self):
        host = self.headers.get("Host", "")
        tenant = server_state.get_tenant_from_host(host)
        self._tenant = tenant
        return tenant

    def _send_json(self, data, status=200):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        """Read JSON body from request."""
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length:
            body = self.rfile.read(content_length)
            return json.loads(body.decode('utf-8'))
        return {}

    def _get_user_from_token(self, tenant_name: str) -> Optional[str]:
        """Get user ID from Authorization header."""
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
            storage = get_tenant_storage(tenant_name)
            return storage["access_tokens"].get(token)
        return None

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_POST(self):
        """Handle POST requests."""
        tenant = self._get_tenant()
        path = urlparse(self.path).path

        if not tenant:
            self._send_json({"errcode": "M_UNKNOWN", "error": "Unknown tenant"}, 404)
            return

        with tenant_context(tenant):
            current = get_current_tenant()
            storage = get_tenant_storage(current.server_name)

            # Register user
            if path == "/_matrix/client/v3/register":
                body = self._read_body()
                username = body.get("username", f"user_{len(storage['users'])}")
                password = body.get("password", "")

                user_id = f"@{username}:{current.server_name}"

                if user_id in storage["users"]:
                    self._send_json({"errcode": "M_USER_IN_USE", "error": "User already exists"}, 400)
                    return

                # Create user
                access_token = hashlib.sha256(f"{user_id}{time.time()}".encode()).hexdigest()[:32]
                storage["users"][user_id] = {"password": password, "created": time.time()}
                storage["access_tokens"][access_token] = user_id

                print(f"  [REGISTER] Created user {user_id} on {current.server_name}")

                self._send_json({
                    "user_id": user_id,
                    "access_token": access_token,
                    "device_id": "DEMO_DEVICE",
                    "home_server": current.server_name,
                    "_tenant_info": {
                        "server_name": current.server_name,
                        "database_schema": current.database_schema,
                    }
                })
                return

            # Login
            if path == "/_matrix/client/v3/login":
                body = self._read_body()
                username = body.get("identifier", {}).get("user", body.get("user", ""))

                if not username.startswith("@"):
                    username = f"@{username}:{current.server_name}"

                access_token = hashlib.sha256(f"{username}{time.time()}".encode()).hexdigest()[:32]
                storage["access_tokens"][access_token] = username

                if username not in storage["users"]:
                    storage["users"][username] = {"created": time.time()}

                print(f"  [LOGIN] User {username} logged in on {current.server_name}")

                self._send_json({
                    "user_id": username,
                    "access_token": access_token,
                    "device_id": "DEMO_DEVICE",
                    "home_server": current.server_name,
                })
                return

            # Create room
            if path == "/_matrix/client/v3/createRoom":
                user_id = self._get_user_from_token(current.server_name)
                if not user_id:
                    self._send_json({"errcode": "M_MISSING_TOKEN", "error": "Missing access token"}, 401)
                    return

                body = self._read_body()
                room_name = body.get("name", "Unnamed Room")
                room_id = f"!{hashlib.sha256(f'{room_name}{time.time()}'.encode()).hexdigest()[:18]}:{current.server_name}"

                storage["rooms"][room_id] = {
                    "name": room_name,
                    "creator": user_id,
                    "members": [user_id],
                    "created": time.time(),
                }
                storage["messages"][room_id] = []

                print(f"  [CREATE_ROOM] {user_id} created room '{room_name}' ({room_id}) on {current.server_name}")

                response = {
                    "room_id": room_id,
                    "_tenant_info": {
                        "server_name": current.server_name,
                        "database_schema": current.database_schema,
                        "stored_in_schema": f"Room stored in {current.database_schema}.rooms",
                    }
                }
                self._send_json(server_state.sign_response(response, current.server_name))
                return

            # Send message
            if path.startswith("/_matrix/client/v3/rooms/") and "/send/" in path:
                user_id = self._get_user_from_token(current.server_name)
                if not user_id:
                    self._send_json({"errcode": "M_MISSING_TOKEN", "error": "Missing access token"}, 401)
                    return

                # Extract room_id from path
                parts = path.split("/")
                room_id_idx = parts.index("rooms") + 1
                room_id = parts[room_id_idx] if room_id_idx < len(parts) else None

                if not room_id or room_id not in storage["rooms"]:
                    self._send_json({"errcode": "M_NOT_FOUND", "error": "Room not found"}, 404)
                    return

                body = self._read_body()
                event_id = f"${ hashlib.sha256(f'{room_id}{time.time()}'.encode()).hexdigest()[:43]}"

                message = {
                    "event_id": event_id,
                    "sender": user_id,
                    "room_id": room_id,
                    "type": "m.room.message",
                    "content": body,
                    "origin_server_ts": int(time.time() * 1000),
                }

                storage["messages"][room_id].append(message)

                print(f"  [MESSAGE] {user_id} sent message in {room_id} on {current.server_name}: {body.get('body', '')[:50]}")

                response = {
                    "event_id": event_id,
                    "_tenant_info": {
                        "server_name": current.server_name,
                        "database_schema": current.database_schema,
                        "stored_in_schema": f"Message stored in {current.database_schema}.events",
                    }
                }
                self._send_json(server_state.sign_response(response, current.server_name))
                return

            self._send_json({"errcode": "M_UNRECOGNIZED", "error": f"Unknown endpoint: {path}"}, 404)

    def do_GET(self):
        tenant = self._get_tenant()
        path = urlparse(self.path).path

        # Health check
        if path == "/health":
            self._send_json({"status": "ok"})
            return

        # Tenant info
        if path == "/_synapse/admin/v1/tenant":
            if tenant:
                with tenant_context(tenant):
                    current = get_current_tenant()
                    self._send_json({
                        "tenant": current.server_name,
                        "database_schema": current.database_schema,
                        "media_store_path": current.media_store_path,
                    })
            else:
                self._send_json({"error": "Unknown tenant"}, 404)
            return

        # Matrix client versions
        if path == "/_matrix/client/versions":
            if not tenant:
                self._send_json({
                    "error": "Unknown tenant",
                    "host": self.headers.get("Host"),
                    "available_tenants": list(server_state.tenants.keys()),
                }, 404)
                return

            with tenant_context(tenant):
                current = get_current_tenant()
                response = {
                    "versions": ["v1.1", "v1.2", "v1.3", "v1.4", "v1.5", "v1.6"],
                    "unstable_features": {},
                    "_tenant_info": {
                        "server_name": current.server_name,
                        "database_schema": current.database_schema,
                        "media_store_path": current.media_store_path,
                    },
                }
                signed = server_state.sign_response(response, current.server_name)
                self._send_json(signed)
            return

        # Well-known
        if path == "/.well-known/matrix/client":
            if not tenant:
                self._send_json({"error": "Unknown tenant"}, 404)
                return

            self._send_json({
                "m.homeserver": {
                    "base_url": f"http://{tenant.server_name}"
                }
            })
            return

        # Get rooms (joined_rooms)
        if path == "/_matrix/client/v3/joined_rooms":
            if not tenant:
                self._send_json({"errcode": "M_UNKNOWN", "error": "Unknown tenant"}, 404)
                return

            with tenant_context(tenant):
                current = get_current_tenant()
                user_id = self._get_user_from_token(current.server_name)
                if not user_id:
                    self._send_json({"errcode": "M_MISSING_TOKEN", "error": "Missing access token"}, 401)
                    return

                storage = get_tenant_storage(current.server_name)
                user_rooms = [rid for rid, room in storage["rooms"].items() if user_id in room.get("members", [])]

                self._send_json({
                    "joined_rooms": user_rooms,
                    "_tenant_info": {
                        "server_name": current.server_name,
                        "database_schema": current.database_schema,
                    }
                })
            return

        # Get messages from room
        if path.startswith("/_matrix/client/v3/rooms/") and "/messages" in path:
            if not tenant:
                self._send_json({"errcode": "M_UNKNOWN", "error": "Unknown tenant"}, 404)
                return

            with tenant_context(tenant):
                current = get_current_tenant()
                user_id = self._get_user_from_token(current.server_name)
                if not user_id:
                    self._send_json({"errcode": "M_MISSING_TOKEN", "error": "Missing access token"}, 401)
                    return

                parts = path.split("/")
                room_id_idx = parts.index("rooms") + 1
                room_id = parts[room_id_idx] if room_id_idx < len(parts) else None

                storage = get_tenant_storage(current.server_name)

                if not room_id or room_id not in storage["rooms"]:
                    self._send_json({"errcode": "M_NOT_FOUND", "error": "Room not found"}, 404)
                    return

                messages = storage["messages"].get(room_id, [])

                self._send_json({
                    "chunk": messages,
                    "start": "start",
                    "end": "end",
                    "_tenant_info": {
                        "server_name": current.server_name,
                        "database_schema": current.database_schema,
                        "messages_from_schema": f"Messages from {current.database_schema}.events",
                    }
                })
            return

        # Tenant stats
        if path == "/_synapse/admin/v1/tenant/stats":
            if not tenant:
                self._send_json({"error": "Unknown tenant"}, 404)
                return

            with tenant_context(tenant):
                current = get_current_tenant()
                storage = get_tenant_storage(current.server_name)

                self._send_json({
                    "tenant": current.server_name,
                    "database_schema": current.database_schema,
                    "stats": {
                        "users": len(storage["users"]),
                        "rooms": len(storage["rooms"]),
                        "messages": sum(len(msgs) for msgs in storage["messages"].values()),
                    }
                })
            return

        # Default response
        if tenant:
            with tenant_context(tenant):
                current = get_current_tenant()
                self._send_json({
                    "message": "Multi-Tenant Synapse Demo",
                    "tenant": current.server_name,
                    "schema": current.database_schema,
                    "path": path,
                    "endpoints": [
                        "GET  /_matrix/client/versions",
                        "GET  /_matrix/client/v3/joined_rooms",
                        "GET  /_matrix/client/v3/rooms/{room_id}/messages",
                        "POST /_matrix/client/v3/register",
                        "POST /_matrix/client/v3/login",
                        "POST /_matrix/client/v3/createRoom",
                        "POST /_matrix/client/v3/rooms/{room_id}/send/{eventType}/{txnId}",
                        "GET  /_synapse/admin/v1/tenant",
                        "GET  /_synapse/admin/v1/tenant/stats",
                        "GET  /.well-known/matrix/client",
                        "GET  /health",
                    ],
                })
        else:
            self._send_json({
                "error": "Unknown tenant",
                "message": "Set Host header to one of the configured tenants",
                "available_tenants": list(server_state.tenants.keys()),
                "host_received": self.headers.get("Host"),
                "example": 'curl -H "Host: acme.localhost" http://localhost:8008/_matrix/client/versions',
            }, 404)


def run_server(port=8008):
    """Run the multi-tenant server."""
    global server_state

    print("\n" + "=" * 60)
    print("  MULTI-TENANT SYNAPSE DEMO SERVER")
    print("=" * 60 + "\n")

    server_state = MultiTenantServer()

    if not server_state.tenants:
        print("\nERROR: No tenants loaded!")
        sys.exit(1)

    server = HTTPServer(("0.0.0.0", port), TenantRequestHandler)

    print(f"\n{'=' * 60}")
    print(f"  Server running on port {port}")
    print(f"{'=' * 60}")
    print("\nTest with:")
    print(f'  curl -H "Host: acme.localhost" http://localhost:{port}/_matrix/client/versions')
    print(f'  curl -H "Host: corp.localhost" http://localhost:{port}/_matrix/client/versions')
    print(f'  curl -H "Host: startup.localhost" http://localhost:{port}/_matrix/client/versions')
    print(f"\n{'=' * 60}\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    run_server()
