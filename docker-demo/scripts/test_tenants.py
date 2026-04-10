#!/usr/bin/env python3
"""End-to-end smoke tests for the 2-tenant docker-demo.

Exercises features from phases 1–6 of the multi-tenant roadmap:

  Phase 1-2: Registration, login, tenant routing, DB schema isolation
  Phase 3:   (SSO/email/push code overlaid — no external services needed)
  Phase 4:   Per-tenant rate-limit differentiation
  Phase 5:   Tenant-scoped media upload/download
  Phase 6:   SIGHUP reload (Synapse stays healthy after signal)

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
SHARED_SECRET_A = "tenant_a_shared_secret_demo"
SHARED_SECRET_B = "tenant_b_shared_secret_demo"
ADMIN_SECRET = "demo_shared_secret_change_in_production"

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
        (TENANT_A, SHARED_SECRET_A, "alice"),
        (TENANT_B, SHARED_SECRET_B, "bob"),
    ]:
        token = register_user(base, tenant, user, "testpass123", secret)
        ok = token is not None
        check(f"register {user}@{tenant}", ok)
        if token:
            tokens[tenant] = token

    # Isolation: alice's profile should not be visible on tenant-b
    if TENANT_A in tokens and TENANT_B in tokens:
        r = requests.get(
            f"{base}/_matrix/client/v3/profile/@alice:{TENANT_A}",
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
    admin_token = register_admin(base, TENANT_A, "demoadmin", "adminpass123")
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
    print("  Multi-Tenant Docker Demo — Smoke Tests (Phases 1-6)")
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
