#!/bin/bash
#
# Start the Multi-Tenant Synapse (REAL Synapse Build)
#
# This script builds and starts a REAL Synapse server from source
# with multi-tenant support patches.
#

set -e

cd "$(dirname "$0")"

echo "========================================"
echo "  Multi-Tenant Synapse (REAL BUILD)"
echo "========================================"
echo ""
echo "This will build REAL Synapse from source with multi-tenant patches."
echo "Building may take several minutes on first run..."
echo ""

# Check Docker
if ! command -v docker &> /dev/null; then
    echo "ERROR: Docker is not installed"
    exit 1
fi

if ! docker info &> /dev/null; then
    echo "ERROR: Docker daemon is not running"
    exit 1
fi

# Clean up any old containers
echo "Cleaning up old containers..."
docker compose down -v 2>/dev/null || true

echo ""
echo "Building and starting containers..."
echo "(This includes compiling Synapse's Rust components)"
echo ""
docker compose up -d --build

echo ""
echo "Waiting for services to initialize..."
echo ""

# Wait for key generation
echo "Waiting for key generation..."
for i in {1..30}; do
    if docker compose logs keygen 2>&1 | grep -q "signing keys generated\|already exists"; then
        echo "  Keys generated!"
        break
    fi
    echo "  Waiting for keys ($i/30)..."
    sleep 2
done

# Wait for schema initialization
echo ""
echo "Waiting for schema initialization..."
for i in {1..30}; do
    if docker compose logs init-schemas 2>&1 | grep -q "schemas initialized\|already exists"; then
        echo "  Schemas initialized!"
        break
    fi
    echo "  Waiting for schemas ($i/30)..."
    sleep 2
done

# Wait for Synapse
echo ""
echo "Waiting for Synapse to start..."
echo "(First startup requires database migration, this may take a while)"
for i in {1..60}; do
    if curl -s http://localhost:8008/health > /dev/null 2>&1; then
        echo "  Synapse is ready!"
        break
    fi
    echo "  Attempt $i/60..."
    sleep 3
done

# Final check
if ! curl -s http://localhost:8008/health > /dev/null 2>&1; then
    echo ""
    echo "ERROR: Synapse did not start properly"
    echo "Check logs with: docker compose logs synapse"
    exit 1
fi

echo ""
echo "========================================"
echo "  REAL Synapse Multi-Tenant is Running!"
echo "========================================"
echo ""
echo "Test Matrix API endpoints:"
echo ""
echo '  # Get server versions (should work for all tenants)'
echo '  curl -H "Host: acme.localhost" http://localhost:8008/_matrix/client/versions | jq .'
echo '  curl -H "Host: corp.localhost" http://localhost:8008/_matrix/client/versions | jq .'
echo '  curl -H "Host: startup.localhost" http://localhost:8008/_matrix/client/versions | jq .'
echo ""
echo "Run integration tests:"
echo "  docker compose run test"
echo ""
echo "View Synapse logs:"
echo "  docker compose logs -f synapse"
echo ""
echo "Connect a Matrix client:"
echo "  - Set homeserver URL to: http://localhost:8008"
echo "  - Use tenant hostname as part of your user ID"
echo "  - Example: @user:acme.localhost"
echo ""
echo "Stop:"
echo "  docker compose down"
echo ""
echo "Stop and clean data:"
echo "  docker compose down -v"
echo ""
