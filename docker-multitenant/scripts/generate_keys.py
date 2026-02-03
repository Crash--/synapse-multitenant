#!/usr/bin/env python3
"""Generate signing keys for all tenants and the main server."""

import os
import sys

# Install signedjson if needed
try:
    from signedjson.key import generate_signing_key, write_signing_keys
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "signedjson", "PyNaCl", "unpaddedbase64"])
    from signedjson.key import generate_signing_key, write_signing_keys

KEYS_DIR = os.environ.get("KEYS_DIR", "/keys")

# Main server + all tenants
TENANTS = [
    "localhost",           # Main server key
    "acme.localhost",      # Tenant 1
    "corp.localhost",      # Tenant 2
    "startup.localhost",   # Tenant 3
]


def main():
    print("=" * 50)
    print("  Generating Signing Keys for Multi-Tenant Synapse")
    print("=" * 50)
    print()

    os.makedirs(KEYS_DIR, exist_ok=True)

    for tenant in TENANTS:
        key_file = os.path.join(KEYS_DIR, f"{tenant.replace('.', '_')}.signing.key")

        if os.path.exists(key_file):
            print(f"  [SKIP] {tenant} - key already exists")
            continue

        # Generate key with a unique key ID based on tenant name
        key_id = "a_" + tenant[:8].replace(".", "")
        key = generate_signing_key(key_id)

        # Write to file
        with open(key_file, "w") as f:
            write_signing_keys(f, [key])

        print(f"  [OK] {tenant} -> {os.path.basename(key_file)}")

    print()
    print(f"All signing keys generated in {KEYS_DIR}/")
    print()


if __name__ == "__main__":
    main()
