#!/usr/bin/env python3
"""Bootstrap users and rooms for the k6 stress test.

Registers 10 users per tenant, creates one room per tenant with all
users joined, and writes test-data.json for k6 to consume.

Usage:
    python3 setup.py [--host localhost] [--port 80]
"""

import argparse
import hashlib
import hmac
import json
import sys
import time

try:
    import requests
except ImportError:
    import subprocess
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "-q", "requests"]
    )
    import requests

GLOBAL_SHARED_SECRET = "demo_shared_secret_change_in_production"

TENANTS = {
    "matrix.tenant-a.com": GLOBAL_SHARED_SECRET,
    "matrix.tenant-b.com": GLOBAL_SHARED_SECRET,
    "matrix.tenant-c.com": GLOBAL_SHARED_SECRET,
    "matrix.tenant-d.com": GLOBAL_SHARED_SECRET,
}

USERS_PER_TENANT = 10
USER_PASSWORD = "stresstest"


def _headers(tenant, token=None):
    h = {"Host": tenant, "Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def wait_for_synapse(base, tenants, max_retries=30):
    """Wait until Synapse responds on all tenant Host headers."""
    print("Waiting for Synapse to be ready on all tenants...")
    for tenant in tenants:
        for i in range(max_retries):
            try:
                r = requests.get(
                    f"{base}/_matrix/client/versions",
                    headers=_headers(tenant),
                    timeout=5,
                )
                if r.status_code == 200:
                    print(f"  [OK] {tenant}")
                    break
            except requests.ConnectionError:
                pass
            time.sleep(2)
        else:
            print(f"  [FAIL] {tenant} not ready after {max_retries * 2}s",
                  file=sys.stderr)
            sys.exit(1)


def register_user(base, tenant, username, password, shared_secret):
    """Register a user via the shared-secret admin endpoint.

    Returns access_token or None.
    """
    # Get nonce
    r = requests.get(
        f"{base}/_synapse/admin/v1/register",
        headers=_headers(tenant),
        timeout=10,
    )
    if r.status_code != 200:
        print(f"    [ERROR] nonce request failed: {r.status_code}", file=sys.stderr)
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
    # User already exists — try logging in instead
    if r.status_code == 400 and r.json().get("errcode") == "M_USER_IN_USE":
        return login_user(base, tenant, username, password)
    print(f"    [ERROR] register {username}@{tenant}: {r.status_code} {r.text}",
          file=sys.stderr)
    return None


def login_user(base, tenant, username, password):
    """Login and return access_token."""
    body = {
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": username},
        "password": password,
    }
    r = requests.post(
        f"{base}/_matrix/client/v3/login",
        headers=_headers(tenant),
        json=body,
        timeout=10,
    )
    if r.status_code == 200:
        return r.json().get("access_token")
    return None


def create_room(base, tenant, token, room_alias_suffix):
    """Create a room and return its room_id."""
    body = {
        "preset": "public_chat",
        "name": f"Stress Test Room ({tenant})",
        "room_alias_name": room_alias_suffix,
    }
    r = requests.post(
        f"{base}/_matrix/client/v3/createRoom",
        headers=_headers(tenant, token),
        json=body,
        timeout=10,
    )
    if r.status_code == 200:
        return r.json()["room_id"]
    # Room alias may already exist — look it up
    if r.status_code == 400 and "M_ROOM_IN_USE" in r.text:
        return resolve_room_alias(base, tenant, token, room_alias_suffix)
    print(f"    [ERROR] createRoom on {tenant}: {r.status_code} {r.text}",
          file=sys.stderr)
    return None


def resolve_room_alias(base, tenant, token, alias_suffix):
    """Resolve a room alias to a room_id."""
    alias = f"%23{alias_suffix}%3A{tenant}"
    r = requests.get(
        f"{base}/_matrix/client/v3/directory/room/{alias}",
        headers=_headers(tenant, token),
        timeout=10,
    )
    if r.status_code == 200:
        return r.json()["room_id"]
    return None


def join_room(base, tenant, token, room_id):
    """Join a user to a room."""
    r = requests.post(
        f"{base}/_matrix/client/v3/join/{room_id}",
        headers=_headers(tenant, token),
        json={},
        timeout=10,
    )
    return r.status_code == 200


def main():
    parser = argparse.ArgumentParser(description="Bootstrap k6 stress test data")
    parser.add_argument("--host", default="localhost",
                        help="Traefik hostname (default: localhost)")
    parser.add_argument("--port", default="80",
                        help="Traefik port (default: 80)")
    args = parser.parse_args()

    base = f"http://{args.host}"
    if args.port != "80":
        base = f"http://{args.host}:{args.port}"

    wait_for_synapse(base, TENANTS.keys())

    test_data = {"tenants": {}}

    for tenant, shared_secret in TENANTS.items():
        print(f"\n--- {tenant} ---")
        tenant_data = {"users": [], "room_id": None}

        # Register users
        for i in range(USERS_PER_TENANT):
            username = f"user{i}"
            print(f"  Registering {username}...", end=" ")
            token = register_user(base, tenant, username, USER_PASSWORD, shared_secret)
            if token:
                print("OK")
                tenant_data["users"].append({
                    "username": username,
                    "password": USER_PASSWORD,
                    "access_token": token,
                })
            else:
                print("FAILED")
                sys.exit(1)

        # Create room via user0
        print(f"  Creating stress-test room...", end=" ")
        room_id = create_room(
            base, tenant, tenant_data["users"][0]["access_token"], "stress-test"
        )
        if room_id:
            print(f"OK ({room_id})")
            tenant_data["room_id"] = room_id
        else:
            print("FAILED")
            sys.exit(1)

        # Join all other users to the room
        for i in range(1, USERS_PER_TENANT):
            user = tenant_data["users"][i]
            print(f"  Joining {user['username']}...", end=" ")
            if join_room(base, tenant, user["access_token"], room_id):
                print("OK")
            else:
                print("FAILED")
                sys.exit(1)

        test_data["tenants"][tenant] = tenant_data

    # Write test-data.json
    output_path = "test-data.json"
    with open(output_path, "w") as f:
        json.dump(test_data, f, indent=2)
    print(f"\nTest data written to {output_path}")
    print(f"Total: {len(TENANTS)} tenants, {USERS_PER_TENANT} users each")


if __name__ == "__main__":
    main()
