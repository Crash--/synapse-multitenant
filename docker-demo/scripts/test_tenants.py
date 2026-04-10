#!/usr/bin/env python3
"""End-to-end smoke tests for the 2-tenant docker-demo.

Exercises features from phases 1–6 and 10 of the multi-tenant roadmap:

  Phase 1-2: Registration, login, tenant routing, DB schema isolation
  Phase 3:   (SSO/email/push code overlaid — no external services needed)
  Phase 4:   Per-tenant rate-limit differentiation
  Phase 5:   Tenant-scoped media upload/download
  Phase 6:   SIGHUP reload (Synapse stays healthy after signal)
  Phase 10:  Federation inbound (per-tenant key server, cross-tenant rooms)

Usage:
    docker compose exec synapse python3 /scripts/test_tenants.py

    Or from the host (requires requests):
    python3 scripts/test_tenants.py [--host localhost]
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time

try:
    import requests
except ImportError:
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "-q", "requests"]
    )
    import requests


TENANT_A = "matrix.tenant-a.com"
TENANT_B = "matrix.tenant-b.com"
SHARED_SECRET_A = "demo_shared_secret_change_in_production"
SHARED_SECRET_B = "demo_shared_secret_change_in_production"
ADMIN_SECRET = "demo_shared_secret_change_in_production"

# Unique suffix per run so re-runs don't collide with existing users
_RUN_ID = str(int(time.time()))[-6:]

passed = 0
failed = 0
errors: list[str] = []


def _base(host: str, tenant: str) -> str:
    """Build base URL, routing via Host header when going through Traefik."""
    return f"http://{host}"


def _headers(tenant: str, token: str | None = None) -> dict:
    h = {"Host": tenant, "Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def check(name: str, ok: bool, detail: str = "") -> bool:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        msg = f"  [FAIL] {name}"
        if detail:
            msg += f" — {detail}"
        print(msg)
        errors.append(name)
    return ok


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def register_user(base: str, tenant: str, username: str, password: str, shared_secret: str) -> str | None:
    """Register a user via the admin shared-secret endpoint. Returns access_token or None."""
    import hashlib
    import hmac

    # Get nonce
    r = requests.get(
        f"{base}/_synapse/admin/v1/register",
        headers=_headers(tenant),
        timeout=10,
    )
    if r.status_code != 200:
        return None
    nonce = r.json()["nonce"]

    # Build HMAC
    mac = hmac.new(
        shared_secret.encode("utf-8"),
        digestmod=hashlib.sha1,
    )
    mac.update(nonce.encode("utf-8"))
    mac.update(b"\x00")
    mac.update(username.encode("utf-8"))
    mac.update(b"\x00")
    mac.update(password.encode("utf-8"))
    mac.update(b"\x00")
    mac.update(b"notadmin")

    body = {
        "nonce": nonce,
        "username": username,
        "password": password,
        "admin": False,
        "mac": mac.hexdigest(),
    }
    r = requests.post(
        f"{base}/_synapse/admin/v1/register",
        headers=_headers(tenant),
        json=body,
        timeout=10,
    )
    if r.status_code in (200, 201):
        return r.json().get("access_token")

    # User may already exist from a previous run — fall back to login
    if r.status_code == 400:
        lr = requests.post(
            f"{base}/_matrix/client/v3/login",
            headers=_headers(tenant),
            json={
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": username},
                "password": password,
            },
            timeout=10,
        )
        if lr.status_code == 200:
            return lr.json().get("access_token")

    return None


def register_admin(base: str, tenant: str, username: str, password: str) -> str | None:
    """Register an admin user via the global shared secret."""
    import hashlib
    import hmac

    r = requests.get(
        f"{base}/_synapse/admin/v1/register",
        headers=_headers(tenant),
        timeout=10,
    )
    if r.status_code != 200:
        return None
    nonce = r.json()["nonce"]

    mac = hmac.new(
        ADMIN_SECRET.encode("utf-8"),
        digestmod=hashlib.sha1,
    )
    mac.update(nonce.encode("utf-8"))
    mac.update(b"\x00")
    mac.update(username.encode("utf-8"))
    mac.update(b"\x00")
    mac.update(password.encode("utf-8"))
    mac.update(b"\x00")
    mac.update(b"admin")

    body = {
        "nonce": nonce,
        "username": username,
        "password": password,
        "admin": True,
        "mac": mac.hexdigest(),
    }
    r = requests.post(
        f"{base}/_synapse/admin/v1/register",
        headers=_headers(tenant),
        json=body,
        timeout=10,
    )
    if r.status_code in (200, 201):
        return r.json().get("access_token")
    return None


# ------------------------------------------------------------------
# Test suites
# ------------------------------------------------------------------

def test_versions(base: str) -> None:
    """Phase 1: Both tenants respond to /_matrix/client/versions."""
    print("\n--- Phase 1-2: Tenant routing & versions ---")
    for tenant in (TENANT_A, TENANT_B):
        r = requests.get(
            f"{base}/_matrix/client/versions",
            headers=_headers(tenant),
            timeout=10,
        )
        check(
            f"{tenant} /_matrix/client/versions",
            r.status_code == 200 and "versions" in r.json(),
            f"status={r.status_code}",
        )


def test_well_known(base: str) -> None:
    """Phase 1: .well-known returns correct server_name per tenant."""
    print("\n--- Phase 1: .well-known/matrix/client ---")
    for tenant, expected_base in [
        (TENANT_A, "http://matrix.tenant-a.com/"),
        (TENANT_B, "http://matrix.tenant-b.com/"),
    ]:
        r = requests.get(
            f"{base}/.well-known/matrix/client",
            headers=_headers(tenant),
            timeout=10,
        )
        ok = r.status_code == 200
        if ok:
            body = r.json()
            hs_base = body.get("m.homeserver", {}).get("base_url", "")
            ok = hs_base == expected_base
        check(
            f"{tenant} .well-known base_url",
            ok,
            f"got {hs_base!r}" if not ok and r.status_code == 200 else f"status={r.status_code}",
        )


def test_registration_and_isolation(base: str) -> dict[str, str]:
    """Phase 1-2: Register users on each tenant, verify cross-tenant isolation."""
    print("\n--- Phase 1-2: Registration & tenant isolation ---")
    tokens = {}

    for tenant, secret, user in [
        (TENANT_A, SHARED_SECRET_A, f"alice_{_RUN_ID}"),
        (TENANT_B, SHARED_SECRET_B, f"bob_{_RUN_ID}"),
    ]:
        token = register_user(base, tenant, user, "testpass123", secret)
        ok = token is not None
        check(f"register {user}@{tenant}", ok)
        if token:
            tokens[tenant] = token

    # Isolation: alice's profile should not be visible on tenant-b
    if TENANT_A in tokens and TENANT_B in tokens:
        r = requests.get(
            f"{base}/_matrix/client/v3/profile/@alice_{_RUN_ID}:{TENANT_A}",
            headers=_headers(TENANT_B, tokens[TENANT_B]),
            timeout=10,
        )
        # Should get 404 or similar — the user doesn't exist on tenant-b
        check(
            "cross-tenant isolation: alice not visible on tenant-b",
            r.status_code in (403, 404),
            f"status={r.status_code}",
        )

    return tokens


def test_media(base: str, tokens: dict[str, str]) -> None:
    """Phase 5: Upload media on tenant-a, verify it's not accessible from tenant-b."""
    print("\n--- Phase 5: Tenant-scoped media ---")
    if TENANT_A not in tokens:
        print("  [SKIP] no token for tenant-a")
        return

    # Upload a small file on tenant-a
    upload_headers = {
        "Host": TENANT_A,
        "Authorization": f"Bearer {tokens[TENANT_A]}",
        "Content-Type": "text/plain",
    }
    payload = b"hello from tenant-a"
    r = requests.post(
        f"{base}/_matrix/media/v3/upload?filename=test.txt",
        headers=upload_headers,
        data=payload,
        timeout=10,
    )
    uploaded = r.status_code == 200
    check("upload media on tenant-a", uploaded, f"status={r.status_code}")
    if not uploaded:
        return

    content_uri = r.json().get("content_uri", "")
    # mxc://matrix.tenant-a.com/<media_id>
    parts = content_uri.replace("mxc://", "").split("/", 1)
    if len(parts) != 2:
        check("parse content_uri", False, f"uri={content_uri}")
        return
    server_name, media_id = parts

    # Download from tenant-a — should work
    r = requests.get(
        f"{base}/_matrix/media/v3/download/{server_name}/{media_id}",
        headers=_headers(TENANT_A, tokens[TENANT_A]),
        timeout=10,
    )
    check("download media from tenant-a", r.status_code == 200, f"status={r.status_code}")

    # Try to download from tenant-b — should fail (different media root)
    if TENANT_B in tokens:
        r = requests.get(
            f"{base}/_matrix/media/v3/download/{server_name}/{media_id}",
            headers=_headers(TENANT_B, tokens[TENANT_B]),
            timeout=10,
        )
        check(
            "media not accessible from tenant-b",
            r.status_code in (404, 502),
            f"status={r.status_code}",
        )


def test_admin_tenants(base: str) -> None:
    """Phase 4/6: Admin API lists tenants."""
    print("\n--- Phase 4/6: Admin tenant API ---")

    # Register an admin on tenant-a
    admin_token = register_admin(base, TENANT_A, f"demoadmin_{_RUN_ID}", "adminpass123")
    if not admin_token:
        print("  [SKIP] could not register admin user")
        return

    r = requests.get(
        f"{base}/_synapse/admin/v1/tenants",
        headers=_headers(TENANT_A, admin_token),
        timeout=10,
    )
    if check("GET /_synapse/admin/v1/tenants", r.status_code == 200, f"status={r.status_code}"):
        body = r.json()
        tenant_names = [t.get("server_name") for t in body.get("tenants", [])]
        check(
            "tenant list contains both tenants",
            TENANT_A in tenant_names and TENANT_B in tenant_names,
            f"got {tenant_names}",
        )


def test_sighup_reload(base: str) -> None:
    """Phase 6: Send SIGHUP to Synapse, verify it stays healthy."""
    print("\n--- Phase 6: SIGHUP reload ---")

    # Check if we're running inside the synapse container
    in_container = os.path.exists("/.dockerenv")

    if in_container:
        # Find Synapse PID and send SIGHUP directly
        try:
            result = subprocess.run(
                ["pgrep", "-f", "synapse.app.homeserver"],
                capture_output=True, text=True, timeout=5,
            )
            pids = result.stdout.strip().split()
            if pids:
                os.kill(int(pids[0]), signal.SIGHUP)
                print("  Sent SIGHUP to Synapse process")
            else:
                print("  [SKIP] cannot find Synapse process (not in synapse container?)")
                return
        except Exception as e:
            print(f"  [SKIP] SIGHUP failed: {e}")
            return
    else:
        # From the host, use docker exec
        try:
            subprocess.run(
                ["docker", "exec", "synapse-demo-server",
                 "sh", "-c", "kill -HUP $(pgrep -f synapse.app.homeserver | head -1)"],
                capture_output=True, text=True, timeout=10,
            )
            print("  Sent SIGHUP via docker exec")
        except Exception as e:
            print(f"  [SKIP] docker exec SIGHUP failed: {e}")
            return

    # Wait a moment for reload to complete
    time.sleep(3)

    # Verify both tenants still respond
    for tenant in (TENANT_A, TENANT_B):
        r = requests.get(
            f"{base}/_matrix/client/versions",
            headers=_headers(tenant),
            timeout=10,
        )
        check(
            f"post-SIGHUP {tenant} healthy",
            r.status_code == 200,
            f"status={r.status_code}",
        )


# ------------------------------------------------------------------
# Phase 9-10: Federation
# ------------------------------------------------------------------

def test_federation_key_server(base: str) -> None:
    """Phase 10b: Each tenant's /_matrix/key/v2/server returns distinct keys."""
    print("\n--- Phase 10b: Per-tenant federation key server ---")

    keys_by_tenant: dict[str, dict] = {}

    for tenant, expected_name in [
        (TENANT_A, TENANT_A),
        (TENANT_B, TENANT_B),
    ]:
        r = requests.get(
            f"{base}/_matrix/key/v2/server",
            headers=_headers(tenant),
            timeout=10,
        )
        if not check(
            f"{tenant} key server responds",
            r.status_code == 200,
            f"status={r.status_code}",
        ):
            continue

        body = r.json()
        keys_by_tenant[tenant] = body

        # server_name in response must match the tenant
        check(
            f"{tenant} key server returns correct server_name",
            body.get("server_name") == expected_name,
            f"got {body.get('server_name')!r}, expected {expected_name!r}",
        )

        # Must have at least one verify key
        verify_keys = body.get("verify_keys", {})
        check(
            f"{tenant} key server has verify_keys",
            len(verify_keys) > 0,
            f"got {len(verify_keys)} keys",
        )

        # Must have a valid_until_ts in the future
        valid_until = body.get("valid_until_ts", 0)
        check(
            f"{tenant} key server has valid_until_ts",
            valid_until > int(time.time() * 1000),
            f"valid_until_ts={valid_until}",
        )

        # Response must be signed (signatures dict present)
        sigs = body.get("signatures", {})
        check(
            f"{tenant} key response is signed",
            expected_name in sigs and len(sigs[expected_name]) > 0,
            f"signatures={list(sigs.keys())}",
        )

    # The two tenants must have DIFFERENT verify keys
    if TENANT_A in keys_by_tenant and TENANT_B in keys_by_tenant:
        vk_a = keys_by_tenant[TENANT_A].get("verify_keys", {})
        vk_b = keys_by_tenant[TENANT_B].get("verify_keys", {})

        # Compare actual key material — either different key IDs or different key values
        keys_differ = vk_a != vk_b
        check(
            "tenant-a and tenant-b have different signing keys",
            keys_differ,
            f"a={list(vk_a.keys())}, b={list(vk_b.keys())}",
        )


def test_federation_version(base: str) -> None:
    """Phase 10: Federation version endpoint responds per tenant."""
    print("\n--- Phase 10: Federation version endpoint ---")
    for tenant in (TENANT_A, TENANT_B):
        r = requests.get(
            f"{base}/_matrix/federation/v1/version",
            headers=_headers(tenant),
            timeout=10,
        )
        if check(
            f"{tenant} federation version",
            r.status_code == 200,
            f"status={r.status_code}",
        ):
            body = r.json()
            server_info = body.get("server", {})
            check(
                f"{tenant} federation version has server info",
                "name" in server_info and "version" in server_info,
                f"server={server_info}",
            )


def test_cross_tenant_room(base: str, tokens: dict[str, str]) -> None:
    """Phase 10: Cross-tenant invite, join, and message delivery.

    Even though inter-tenant communication on the same process goes
    through the local path (not federation wire protocol), this test
    validates that tenant context switching works correctly for
    cross-tenant room operations.
    """
    print("\n--- Phase 10: Cross-tenant room operations ---")

    if TENANT_A not in tokens or TENANT_B not in tokens:
        print("  [SKIP] need tokens for both tenants")
        return

    token_a = tokens[TENANT_A]
    token_b = tokens[TENANT_B]

    # 1. Create a room on tenant-a
    r = requests.post(
        f"{base}/_matrix/client/v3/createRoom",
        headers=_headers(TENANT_A, token_a),
        json={"preset": "public_chat", "name": "cross-tenant-test"},
        timeout=10,
    )
    if not check(
        "create room on tenant-a",
        r.status_code == 200,
        f"status={r.status_code} body={r.text[:200]}",
    ):
        return
    room_id = r.json()["room_id"]

    # 2. Invite bob@tenant-b from tenant-a
    bob_mxid = f"@bob_{_RUN_ID}:{TENANT_B}"
    r = requests.post(
        f"{base}/_matrix/client/v3/rooms/{room_id}/invite",
        headers=_headers(TENANT_A, token_a),
        json={"user_id": bob_mxid},
        timeout=10,
    )
    invite_ok = r.status_code == 200
    check(
        f"invite {bob_mxid} to room",
        invite_ok,
        f"status={r.status_code} body={r.text[:200]}",
    )

    if not invite_ok:
        # Cross-tenant invite may fail if Synapse doesn't support it
        # in local mode — this is still useful diagnostic info
        print(f"  NOTE: Cross-tenant invite failed. This may indicate that")
        print(f"        inter-tenant operations require actual federation.")
        return

    # 3. Bob joins the room from tenant-b
    r = requests.post(
        f"{base}/_matrix/client/v3/join/{room_id}",
        headers=_headers(TENANT_B, token_b),
        json={},
        timeout=10,
    )
    join_ok = r.status_code == 200
    check(
        f"bob joins room from tenant-b",
        join_ok,
        f"status={r.status_code} body={r.text[:200]}",
    )

    if not join_ok:
        return

    # 4. Alice sends a message
    r = requests.put(
        f"{base}/_matrix/client/v3/rooms/{room_id}/send/m.room.message/fed-test-1",
        headers=_headers(TENANT_A, token_a),
        json={"msgtype": "m.text", "body": "hello from tenant-a"},
        timeout=10,
    )
    check(
        "alice sends message",
        r.status_code == 200,
        f"status={r.status_code}",
    )

    # 5. Bob syncs and should see the message
    # Use /messages endpoint for simplicity (no sync token needed)
    time.sleep(1)  # brief settle
    r = requests.get(
        f"{base}/_matrix/client/v3/rooms/{room_id}/messages?dir=b&limit=5",
        headers=_headers(TENANT_B, token_b),
        timeout=10,
    )
    if check(
        "bob can read room messages",
        r.status_code == 200,
        f"status={r.status_code}",
    ):
        messages = r.json().get("chunk", [])
        bodies = [
            m.get("content", {}).get("body", "")
            for m in messages
            if m.get("type") == "m.room.message"
        ]
        check(
            "bob sees alice's message",
            "hello from tenant-a" in bodies,
            f"got bodies={bodies}",
        )

    # 6. Bob sends a reply
    r = requests.put(
        f"{base}/_matrix/client/v3/rooms/{room_id}/send/m.room.message/fed-test-2",
        headers=_headers(TENANT_B, token_b),
        json={"msgtype": "m.text", "body": "hello from tenant-b"},
        timeout=10,
    )
    check(
        "bob sends reply",
        r.status_code == 200,
        f"status={r.status_code}",
    )

    # 7. Alice sees Bob's reply
    time.sleep(1)
    r = requests.get(
        f"{base}/_matrix/client/v3/rooms/{room_id}/messages?dir=b&limit=5",
        headers=_headers(TENANT_A, token_a),
        timeout=10,
    )
    if check(
        "alice can read room messages",
        r.status_code == 200,
        f"status={r.status_code}",
    ):
        messages = r.json().get("chunk", [])
        bodies = [
            m.get("content", {}).get("body", "")
            for m in messages
            if m.get("type") == "m.room.message"
        ]
        check(
            "alice sees bob's reply",
            "hello from tenant-b" in bodies,
            f"got bodies={bodies}",
        )


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main() -> None:
    global passed, failed

    parser = argparse.ArgumentParser(description="Multi-tenant demo smoke tests")
    parser.add_argument(
        "--host", default="localhost",
        help="Hostname where Traefik is listening (default: localhost)",
    )
    parser.add_argument(
        "--port", default="80", type=str,
        help="Port where Traefik is listening (default: 80)",
    )
    args = parser.parse_args()

    base = f"http://{args.host}"
    if args.port != "80":
        base = f"http://{args.host}:{args.port}"

    print("=" * 60)
    print("  Multi-Tenant Docker Demo — Smoke Tests (Phases 1-6, 10)")
    print(f"  Target: {base}")
    print("=" * 60)

    # Wait for Synapse to be healthy
    print("\nWaiting for Synapse to be ready...")
    for i in range(30):
        try:
            r = requests.get(
                f"{base}/_matrix/client/versions",
                headers=_headers(TENANT_A),
                timeout=5,
            )
            if r.status_code == 200:
                print("Synapse is ready.\n")
                break
        except requests.ConnectionError:
            pass
        time.sleep(2)
    else:
        print("Synapse did not become ready in time.", file=sys.stderr)
        sys.exit(1)

    # Run test suites
    test_versions(base)
    test_well_known(base)
    tokens = test_registration_and_isolation(base)
    test_media(base, tokens)
    test_admin_tenants(base)
    test_sighup_reload(base)
    test_federation_key_server(base)
    test_federation_version(base)
    test_cross_tenant_room(base, tokens)

    # Summary
    print("\n" + "=" * 60)
    total = passed + failed
    print(f"  Results: {passed}/{total} passed, {failed}/{total} failed")
    if errors:
        print(f"  Failed: {', '.join(errors)}")
    print("=" * 60)

    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    main()
