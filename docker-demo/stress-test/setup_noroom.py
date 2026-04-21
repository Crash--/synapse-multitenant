#!/usr/bin/env python3
"""Bootstrap users for the no-room stress test variant.

Skips createRoom (which trips a known state-group persistence bug in
this multi-tenant fork). Writes test-data.json with users only.
"""
import argparse
import hashlib
import hmac
import json
import sys
import time

import requests

requests.packages.urllib3.disable_warnings(
    requests.packages.urllib3.exceptions.InsecureRequestWarning
)
s = requests.Session()
s.verify = False

GLOBAL_SHARED_SECRET = "demo_shared_secret_change_in_production"
CONTROL_PLANE_TOKEN = "demo-control-plane-token"
CONTROL_PLANE_HOST = "control-plane.localhost"
DEFAULT_USERS_PER_TENANT = 10
USER_PASSWORD = "stresstest"

# For N=4 (the existing default) we preserve the `stress-a..d` names used
# in docs/results/2026-04-21. For N>4 (density tests) we switch to a
# zero-padded numeric scheme `stress-NNN.localhost` so the same harness
# scales to tens or hundreds of tenants.
def generate_tenant_names(n: int) -> list[str]:
    if n <= 0:
        raise ValueError("tenant count must be >= 1")
    if n == 4:
        return [f"stress-{c}.localhost" for c in "abcd"]
    width = max(3, len(str(n)))
    return [f"stress-{i + 1:0{width}d}.localhost" for i in range(n)]


def hdrs(tenant, token=None):
    h = {"Host": tenant, "Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def login(base, tenant, username, password, retries=4):
    for attempt in range(retries):
        r = s.post(
            f"{base}/_matrix/client/v3/login",
            headers=hdrs(tenant),
            json={
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": username},
                "password": password,
            },
            timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("access_token")
        time.sleep(0.3 * (attempt + 1))
    return None


def register(base, tenant, username, password, secret, retries=4):
    last = None
    for attempt in range(retries):
        nr = s.get(
            f"{base}/_synapse/admin/v1/register",
            headers=hdrs(tenant),
            timeout=10,
        )
        if nr.status_code != 200:
            last = f"nonce={nr.status_code}"
            time.sleep(0.3 * (attempt + 1))
            continue
        nonce = nr.json()["nonce"]
        m = hmac.new(secret.encode(), digestmod=hashlib.sha1)
        for piece in (nonce, username, password):
            m.update(piece.encode())
            m.update(b"\x00")
        m.update(b"notadmin")
        rr = s.post(
            f"{base}/_synapse/admin/v1/register",
            headers=hdrs(tenant),
            json={
                "nonce": nonce,
                "username": username,
                "password": password,
                "admin": False,
                "mac": m.hexdigest(),
            },
            timeout=10,
        )
        if rr.status_code in (200, 201):
            return rr.json().get("access_token")
        body = rr.json() if rr.text else {}
        if rr.status_code == 400 and body.get("errcode") == "M_USER_IN_USE":
            return login(base, tenant, username, password)
        last = f"{rr.status_code} {rr.text[:120]}"
        time.sleep(0.3 * (attempt + 1))
    print(f"    [ERROR] {username}@{tenant}: {last}", file=sys.stderr)
    return None


def provision_tenant(base, server_name, retries=3):
    """Provision a tenant via the control plane. Idempotent — 409 on
    existing tenants is treated as success."""
    for attempt in range(retries):
        r = s.post(
            f"{base}/api/v1/tenants",
            headers={
                "Host": CONTROL_PLANE_HOST,
                "Content-Type": "application/json",
                "Authorization": f"Bearer {CONTROL_PLANE_TOKEN}",
            },
            json={"server_name": server_name, "registration_enabled": True},
            timeout=30,
        )
        if r.status_code in (200, 201):
            return True
        if r.status_code == 409:
            return True  # already exists — fine
        time.sleep(0.5 * (attempt + 1))
    print(
        f"    [ERROR] provision {server_name}: {r.status_code} {r.text[:200]}",
        file=sys.stderr,
    )
    return False


def main():
    p = argparse.ArgumentParser(
        description="Provision N tenants, register K users each, write test-data.json"
    )
    p.add_argument("--base", default="https://localhost")
    p.add_argument(
        "--tenants",
        type=int,
        default=4,
        help="number of tenants to provision (default 4, for density tests use e.g. 50)",
    )
    p.add_argument(
        "--users-per-tenant",
        type=int,
        default=DEFAULT_USERS_PER_TENANT,
        help="number of users per tenant (default 10)",
    )
    p.add_argument(
        "--skip-provision",
        action="store_true",
        help="assume tenants are already provisioned; only register users",
    )
    args = p.parse_args()

    tenants = generate_tenant_names(args.tenants)
    users_per = args.users_per_tenant

    # Phase 1: provision tenants (idempotent).
    if not args.skip_provision:
        print(f"=== provisioning {len(tenants)} tenants ===")
        for t in tenants:
            print(f"  {t}...", end=" ", flush=True)
            if not provision_tenant(args.base, t):
                print("FAIL")
                sys.exit(1)
            print("OK")
        # Give the control plane a moment to /reload Synapse's registry.
        time.sleep(2)

    # Phase 2: register users.
    data = {"tenants": {}}
    for idx, t in enumerate(tenants, 1):
        print(f"\n=== [{idx}/{len(tenants)}] {t} ===")
        users = []
        for i in range(users_per):
            u = f"user{i}"
            print(f"  {u}...", end=" ", flush=True)
            tok = register(args.base, t, u, USER_PASSWORD, GLOBAL_SHARED_SECRET)
            if not tok:
                print("FAIL")
                sys.exit(1)
            print("OK")
            users.append(
                {"username": u, "password": USER_PASSWORD, "access_token": tok}
            )
        data["tenants"][t] = {"users": users, "room_id": None}

    with open("test-data.json", "w") as f:
        json.dump(data, f, indent=2)
    print(
        f"\nWrote test-data.json: {len(tenants)} tenants x {users_per} users"
    )


if __name__ == "__main__":
    main()
