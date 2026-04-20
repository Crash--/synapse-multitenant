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
TENANTS = [
    "stress-a.localhost",
    "stress-b.localhost",
    "stress-c.localhost",
    "stress-d.localhost",
]
USERS_PER_TENANT = 10
USER_PASSWORD = "stresstest"


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


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="https://localhost")
    args = p.parse_args()

    data = {"tenants": {}}
    for t in TENANTS:
        print(f"\n=== {t} ===")
        users = []
        for i in range(USERS_PER_TENANT):
            u = f"user{i}"
            print(f"  {u}...", end=" ", flush=True)
            tok = register(args.base, t, u, USER_PASSWORD, GLOBAL_SHARED_SECRET)
            if not tok:
                print("FAIL")
                sys.exit(1)
            print("OK")
            users.append({"username": u, "password": USER_PASSWORD, "access_token": tok})
        data["tenants"][t] = {"users": users, "room_id": None}

    with open("test-data.json", "w") as f:
        json.dump(data, f, indent=2)
    print(
        f"\nWrote test-data.json: {len(TENANTS)} tenants x {USERS_PER_TENANT} users"
    )


if __name__ == "__main__":
    main()
