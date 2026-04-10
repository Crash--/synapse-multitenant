#!/usr/bin/env python3
"""Register an admin user on a tenant via the shared-secret admin API."""

import hashlib
import hmac
import json
import os
import sys
import urllib.request
import urllib.error

SYNAPSE_HOST = os.environ.get("SYNAPSE_HOST", "http://synapse:8008")
TENANT_HOST = os.environ.get("TENANT_HOST", "matrix.tenant-a.com")
USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
SHARED_SECRET = os.environ.get("SHARED_SECRET", "")

url = f"{SYNAPSE_HOST}/_synapse/admin/v1/register"
headers = {"Host": TENANT_HOST, "Content-Type": "application/json"}


def api(data=None):
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode() if data else None,
        headers=headers,
        method="POST" if data else "GET",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = json.loads(e.read())
        if e.code == 400 and body.get("errcode") == "M_USER_IN_USE":
            print(f"Admin user @{USERNAME}:{TENANT_HOST} already exists, skipping.")
            sys.exit(0)
        raise


# Step 1: get nonce
nonce = api()["nonce"]

# Step 2: compute HMAC
mac = hmac.new(SHARED_SECRET.encode(), digestmod=hashlib.sha1)
mac.update(nonce.encode())
mac.update(b"\x00")
mac.update(USERNAME.encode())
mac.update(b"\x00")
mac.update(PASSWORD.encode())
mac.update(b"\x00")
mac.update(b"admin")

# Step 3: register
result = api(
    {
        "nonce": nonce,
        "username": USERNAME,
        "password": PASSWORD,
        "admin": True,
        "mac": mac.hexdigest(),
    }
)

print(f"Registered admin user: {result.get('user_id')}")
