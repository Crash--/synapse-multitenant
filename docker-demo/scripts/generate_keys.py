#!/usr/bin/env python3
"""Generate Ed25519 signing keys for the demo's primary server + tenants.

The list of server names is taken from the ``TENANTS`` env var, and each
key is written under ``KEYS_DIR`` (default ``/keys``) using the same
``server_name.replace('.', '_') + '.signing.key'`` filename convention
the demo's homeserver.yaml expects.
"""

import os
import sys

try:
    from signedjson.key import generate_signing_key, write_signing_keys
except ImportError:
    import subprocess
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "-q",
         "signedjson", "PyNaCl", "unpaddedbase64"]
    )
    from signedjson.key import generate_signing_key, write_signing_keys


KEYS_DIR = os.environ.get("KEYS_DIR", "/keys")
TENANTS = [
    name.strip()
    for name in os.environ.get(
        "TENANTS",
        "localhost,matrix.tenant-a.com,matrix.tenant-b.com",
    ).split(",")
    if name.strip()
]


def main() -> None:
    print("=" * 50)
    print("  Generating signing keys for the demo")
    print("=" * 50)

    os.makedirs(KEYS_DIR, exist_ok=True)

    for tenant in TENANTS:
        key_file = os.path.join(KEYS_DIR, f"{tenant.replace('.', '_')}.signing.key")
        if os.path.exists(key_file):
            print(f"  [SKIP] {tenant} — key already exists")
            continue
        key_id = "a_" + tenant[:8].replace(".", "").replace("-", "")
        key = generate_signing_key(key_id)
        with open(key_file, "w") as f:
            write_signing_keys(f, [key])
        print(f"  [OK]   {tenant} -> {os.path.basename(key_file)}")

    print(f"\nKeys written to {KEYS_DIR}/")


if __name__ == "__main__":
    main()
