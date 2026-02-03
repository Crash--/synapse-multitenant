#!/usr/bin/env python3
"""
Generate signing keys for multi-tenant Synapse.

This script generates Ed25519 signing keys for each configured tenant.
"""

import os
import sys

from signedjson.key import generate_signing_key, write_signing_keys


def generate_tenant_key(tenant_name: str, output_path: str) -> None:
    """Generate a signing key for a tenant.

    Args:
        tenant_name: The tenant server name (used in key ID)
        output_path: Path to write the key file
    """
    if os.path.exists(output_path):
        print(f"Key already exists for {tenant_name}: {output_path}")
        return

    # Generate a new signing key
    key = generate_signing_key("a_" + tenant_name[:10].replace(".", ""))

    # Write the key to file
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        write_signing_keys(f, [key])

    print(f"Generated signing key for {tenant_name}: {output_path}")


def main():
    """Generate keys for configured tenants."""
    # Tenant configurations - matches what's in homeserver.yaml
    tenants = os.environ.get("TENANTS", "acme.localhost,corp.localhost,startup.localhost").split(",")
    keys_dir = os.environ.get("KEYS_DIR", "/keys")

    for tenant in tenants:
        tenant = tenant.strip()
        if not tenant:
            continue

        # Sanitize tenant name for filename
        safe_name = tenant.replace(".", "_").replace(":", "_")
        key_path = os.path.join(keys_dir, f"{safe_name}.signing.key")

        try:
            generate_tenant_key(tenant, key_path)
        except Exception as e:
            print(f"Error generating key for {tenant}: {e}", file=sys.stderr)
            sys.exit(1)

    print(f"All signing keys generated in {keys_dir}")


if __name__ == "__main__":
    main()
