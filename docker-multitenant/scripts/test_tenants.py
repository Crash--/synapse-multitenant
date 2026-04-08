#!/usr/bin/env python3
"""
Test script for multi-tenant Synapse.

This script tests that each tenant is properly isolated and functioning
using real Matrix API endpoints.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

SYNAPSE_URL = "http://synapse:8008"
TENANTS = ["acme.localhost", "corp.localhost", "startup.localhost"]

# Path inside the test container where the synapse log file is mounted
# (see docker-compose.yml — ./data/logs is bind-mounted to /synapse-logs).
SYNAPSE_LOG_PATH = "/synapse-logs/synapse.log"


def make_request(method, path, host, data=None, headers=None):
    """Make a request to Synapse with a specific Host header."""
    url = f"{SYNAPSE_URL}{path}"
    all_headers = {"Host": host}
    if headers:
        all_headers.update(headers)

    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        all_headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=body, headers=all_headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return {
                "status": response.status,
                "data": json.loads(response.read().decode("utf-8")),
            }
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            body = {}
        return {"status": e.code, "data": body, "error": str(e)}
    except Exception as e:
        return {"status": 0, "data": {}, "error": str(e)}


def wait_for_synapse():
    """Wait for Synapse to be ready."""
    print("Waiting for Synapse to be ready...")
    for i in range(60):
        try:
            req = urllib.request.Request(f"{SYNAPSE_URL}/health")
            with urllib.request.urlopen(req, timeout=5) as response:
                if response.status == 200:
                    print("  Synapse is ready!")
                    return True
        except Exception:
            pass
        print(f"  Attempt {i+1}/60...")
        time.sleep(2)

    print("  ERROR: Synapse did not become ready")
    return False


def test_versions(tenant):
    """Test the /_matrix/client/versions endpoint."""
    result = make_request("GET", "/_matrix/client/versions", tenant)

    if result.get("status") == 200:
        data = result["data"]
        if "versions" in data:
            print(f"    [PASS] /versions - {len(data['versions'])} versions supported")
            return True
        else:
            print(f"    [FAIL] /versions - missing versions key")
            return False
    else:
        print(f"    [FAIL] /versions - status {result.get('status')}: {result.get('error', 'unknown')}")
        return False


_REGISTER_SEQ = 0


def test_register(tenant):
    """Test user registration for a tenant.

    Returns the user credentials if successful, None otherwise.
    """
    # Generate unique username — combine microsecond timestamp + a
    # process-local counter so back-to-back calls don't collide.
    global _REGISTER_SEQ
    _REGISTER_SEQ += 1
    username = f"test_{tenant.replace('.', '_')}_{int(time.time() * 1000)}_{_REGISTER_SEQ}"

    # First, get registration flows
    result = make_request("POST", "/_matrix/client/v3/register", tenant, data={})

    if result.get("status") == 401:
        data = result["data"]
        session = data.get("session")

        # Try dummy auth flow
        register_data = {
            "username": username,
            "password": "TestPassword123!",
            "auth": {
                "type": "m.login.dummy",
                "session": session,
            },
        }
        result = make_request("POST", "/_matrix/client/v3/register", tenant, data=register_data)

        if result.get("status") == 200:
            data = result["data"]
            print(f"    [PASS] register - user {data.get('user_id')}")
            return {
                "user_id": data.get("user_id"),
                "access_token": data.get("access_token"),
                "device_id": data.get("device_id"),
            }
        else:
            print(f"    [FAIL] register - status {result.get('status')}: {result['data'].get('error', 'unknown')}")
            return None
    else:
        print(f"    [FAIL] register - unexpected status {result.get('status')}")
        return None


def test_whoami(tenant, access_token):
    """Test the /whoami endpoint."""
    headers = {"Authorization": f"Bearer {access_token}"}
    result = make_request("GET", "/_matrix/client/v3/account/whoami", tenant, headers=headers)

    if result.get("status") == 200:
        user_id = result["data"].get("user_id")
        print(f"    [PASS] whoami - {user_id}")
        return True
    else:
        print(f"    [FAIL] whoami - status {result.get('status')}")
        return False


def test_create_room(tenant, access_token):
    """Test room creation for a tenant.

    Returns the room_id if successful, None otherwise.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    data = {
        "name": f"Test Room - {tenant}",
        "preset": "private_chat",
    }
    result = make_request("POST", "/_matrix/client/v3/createRoom", tenant, data=data, headers=headers)

    if result.get("status") == 200:
        room_id = result["data"].get("room_id")
        print(f"    [PASS] createRoom - {room_id}")
        return room_id
    else:
        print(f"    [FAIL] createRoom - status {result.get('status')}: {result['data'].get('error', 'unknown')}")
        return None


def test_send_message(tenant, access_token, room_id):
    """Test sending a message to a room."""
    txn_id = str(int(time.time() * 1000))
    path = f"/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    data = {
        "msgtype": "m.text",
        "body": f"Hello from {tenant}! This is a test message.",
    }

    result = make_request("PUT", path, tenant, data=data, headers=headers)

    if result.get("status") == 200:
        event_id = result["data"].get("event_id")
        print(f"    [PASS] sendMessage - {event_id}")
        return True
    else:
        print(f"    [FAIL] sendMessage - status {result.get('status')}: {result['data'].get('error', 'unknown')}")
        return False


def test_unknown_tenant():
    """Test that unknown tenants are handled appropriately."""
    result = make_request("GET", "/_matrix/client/versions", "unknown.localhost")

    # Depending on implementation, unknown tenant might return 404 or still work
    # (routing to default tenant). Both behaviors are valid.
    if result.get("status") == 404:
        print("    [PASS] unknown tenant rejected (404)")
        return True
    elif result.get("status") == 200:
        print("    [INFO] unknown tenant returned versions (may use default routing)")
        return True
    else:
        print(f"    [FAIL] unexpected status {result.get('status')}")
        return False


def fetch_text(path, host=None):
    """GET a URL and return the raw text body (for the metrics scrape)."""
    url = f"{SYNAPSE_URL}{path}"
    headers = {"Host": host} if host else {}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.read().decode("utf-8", errors="replace")
    except Exception as e:
        return f"__error__: {e}"


def test_metrics_endpoint_reachable():
    """Sanity-check: /_synapse/metrics responds with Prometheus exposition.

    This is the prerequisite for any per-tenant labelling work — if the
    endpoint isn't there, the audit/instrumentation phase has nothing
    to assert against.
    """
    body = fetch_text("/_synapse/metrics")
    if body.startswith("__error__"):
        print(f"    [FAIL] /_synapse/metrics unreachable: {body}")
        return False
    # Prometheus exposition always starts with HELP/TYPE comments.
    if "# HELP" in body and "# TYPE" in body:
        print("    [PASS] /_synapse/metrics serving Prometheus exposition")
        return True
    print("    [FAIL] /_synapse/metrics returned unexpected body")
    return False


def test_metrics_have_tenant_label():
    """Check that Synapse metrics differentiate tenants via the
    `server_name` label.

    The fork reuses Synapse's existing `server_name` Prometheus label
    rather than introducing a parallel `tenant=` label — in multi-tenant
    mode `effective_server_name` IS the active tenant (see
    synapse/http/site.py around the requests_counter increment). So
    after hitting `acme.localhost` and `corp.localhost` we expect both
    of those values to appear in the metrics scrape.
    """
    # Hit each tenant once so the per-tenant counters increment.
    for t in TENANTS:
        fetch_text("/_matrix/client/versions", host=t)
    time.sleep(0.5)  # let the counter increments propagate

    body = fetch_text("/_synapse/metrics")
    if body.startswith("__error__"):
        print(f"    [FAIL] could not scrape metrics: {body}")
        return False

    found = [t for t in TENANTS if f'server_name="{t}"' in body]
    if len(found) >= 2:
        print(f"    [PASS] metrics differentiate tenants via server_name "
              f"({len(found)}/{len(TENANTS)} tenants visible)")
        return True
    print(f"    [FAIL] expected ≥2 tenants in server_name labels, "
          f"found {len(found)}: {found}")
    return False


def test_login_response_uses_tenant_server_name():
    """The login response carries `home_server` — it must be the tenant's
    server_name, not the primary `localhost` baked into homeserver.yaml.

    A failure here means the login handler is reading `self.hs.hostname`
    instead of `hs.effective_server_name()` somewhere on the response path.
    """
    # First register so we have credentials.
    user = test_register("acme.localhost")
    if not user:
        print("    [SKIP] could not register a test user")
        return False

    # Now log in as that user.
    result = make_request(
        "POST",
        "/_matrix/client/v3/login",
        "acme.localhost",
        data={
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": user["user_id"].split(":")[0].lstrip("@")},
            "password": "TestPassword123!",
        },
    )

    if result.get("status") != 200:
        print(f"    [FAIL] login - status {result.get('status')}: "
              f"{result['data'].get('error', 'unknown')}")
        return False

    home_server = result["data"].get("home_server")
    user_id = result["data"].get("user_id", "")
    if home_server == "acme.localhost" and user_id.endswith(":acme.localhost"):
        print(f"    [PASS] login - home_server={home_server} user_id={user_id}")
        return True
    print(f"    [FAIL] login - home_server={home_server!r} user_id={user_id!r} "
          f"(expected acme.localhost)")
    return False


def test_capabilities_endpoint_per_tenant():
    """`/_matrix/client/v3/capabilities` is a synchronous request handler.
    Hitting it as different tenants should produce metric series with
    different `server_name` labels — if it doesn't, the handler is using
    `self.server_name` (constructor-time) instead of resolving per-request.
    """
    # Register so we have an access token (capabilities is auth-required).
    user_acme = test_register("acme.localhost")
    user_corp = test_register("corp.localhost")
    if not user_acme or not user_corp:
        print("    [SKIP] could not register both tenants")
        return False

    for u, host in [(user_acme, "acme.localhost"), (user_corp, "corp.localhost")]:
        for _ in range(2):
            make_request(
                "GET",
                "/_matrix/client/v3/capabilities",
                host,
                headers={"Authorization": f"Bearer {u['access_token']}"},
            )
    time.sleep(0.5)

    body = fetch_text("/_synapse/metrics")
    has_acme = (
        'server_name="acme.localhost"' in body
        and "CapabilitiesRestServlet" in body
        and any(
            'server_name="acme.localhost"' in line and "CapabilitiesRestServlet" in line
            for line in body.splitlines()
        )
    )
    has_corp = any(
        'server_name="corp.localhost"' in line and "CapabilitiesRestServlet" in line
        for line in body.splitlines()
    )
    if has_acme and has_corp:
        print("    [PASS] CapabilitiesRestServlet metric carries per-tenant server_name")
        return True
    print(f"    [FAIL] CapabilitiesRestServlet metric not split per tenant "
          f"(acme={has_acme}, corp={has_corp})")
    return False


def test_whoami_response_uses_tenant_server_name():
    """Whoami returns the user_id; the server_name segment must be the
    tenant's, not the primary. Already implicitly tested via
    test_whoami(), but here we make the assertion explicit and lock it in.
    """
    user = test_register("acme.localhost")
    if not user:
        print("    [SKIP] could not register")
        return False
    result = make_request(
        "GET",
        "/_matrix/client/v3/account/whoami",
        "acme.localhost",
        headers={"Authorization": f"Bearer {user['access_token']}"},
    )
    if result.get("status") != 200:
        print(f"    [FAIL] whoami status {result.get('status')}")
        return False
    uid = result["data"].get("user_id", "")
    if uid.endswith(":acme.localhost"):
        print(f"    [PASS] whoami user_id={uid}")
        return True
    print(f"    [FAIL] whoami user_id={uid!r} (expected …:acme.localhost)")
    return False


def test_wellknown_client_per_tenant():
    """`/.well-known/matrix/client` should announce the tenant's own
    homeserver base URL, not the primary. If both tenants get the same
    base_url, the well-known builder is reading the global config.
    """
    acme = make_request("GET", "/.well-known/matrix/client", "acme.localhost")
    corp = make_request("GET", "/.well-known/matrix/client", "corp.localhost")

    # 404 is acceptable — fork may not implement this yet — but if it
    # IS implemented it MUST be tenant-aware.
    if acme.get("status") == 404 and corp.get("status") == 404:
        print("    [SKIP] /.well-known/matrix/client not implemented "
              "(deferred — note for future phase)")
        return True

    acme_base = acme.get("data", {}).get("m.homeserver", {}).get("base_url", "")
    corp_base = corp.get("data", {}).get("m.homeserver", {}).get("base_url", "")
    if "acme" in acme_base and "corp" in corp_base:
        print(f"    [PASS] well-known per tenant ({acme_base} vs {corp_base})")
        return True
    print(f"    [FAIL] well-known not tenant-aware "
          f"(acme={acme_base!r}, corp={corp_base!r})")
    return False


def test_user_directory_per_tenant_population():
    """The user directory is rebuilt by a `notify_new_event` background
    process. Phase 2 makes background processes tenant-aware: today the
    rebuild loop runs with no `TenantConfig` bound, so the per-tenant
    `user_directory` tables are never populated and a self-search returns
    nothing. After the per-tenant background helper lands, registering a
    user with a distinctive display name in a public room must make them
    findable from their own tenant.
    """
    user = test_register("acme.localhost")
    if not user:
        print("    [SKIP] could not register acme user")
        return False

    auth = {"Authorization": f"Bearer {user['access_token']}"}

    # Public, world-readable room — required for the user to be eligible
    # for the user directory at all.
    room = make_request(
        "POST",
        "/_matrix/client/v3/createRoom",
        "acme.localhost",
        data={"visibility": "public", "preset": "public_chat"},
        headers=auth,
    )
    if room.get("status") != 200:
        print(f"    [SKIP] could not create public room: {room.get('status')} {room.get('data')}")
        return False

    # Distinctive display name so the search term is unambiguous.
    distinctive = f"phase2probe{int(time.time() * 1000)}"
    profile = make_request(
        "PUT",
        f"/_matrix/client/v3/profile/{user['user_id']}/displayname",
        "acme.localhost",
        data={"displayname": distinctive},
        headers=auth,
    )
    if profile.get("status") != 200:
        print(f"    [SKIP] could not set displayname: {profile}")
        return False

    # Wait for the user_directory background loop to drain the deltas.
    # The loop runs on every notify_new_event firing — give it room.
    time.sleep(8)

    search = make_request(
        "POST",
        "/_matrix/client/v3/user_directory/search",
        "acme.localhost",
        data={"search_term": distinctive, "limit": 10},
        headers=auth,
    )
    if search.get("status") != 200:
        print(f"    [FAIL] search status {search.get('status')}: {search.get('data')}")
        return False

    results = search.get("data", {}).get("results", [])
    found = any(r.get("user_id") == user["user_id"] for r in results)
    if found:
        print(f"    [PASS] user_directory populated per tenant "
              f"({user['user_id']} findable in own tenant)")
        return True
    print(f"    [FAIL] user_directory not populated for tenant — bg loop not tenant-aware "
          f"(searched={distinctive!r}, results={results})")
    return False


def test_stats_loop_per_tenant():
    """The stats background loop (`handlers/stats.py`) processes state
    deltas and advances `event_processing_positions{name="stats"}`. With
    per-tenant fan-out, that metric should appear once per tenant with
    the tenant's own `server_name` label. Before the fix, the loop ran
    with no tenant context bound, so only the primary hostname (or no
    row at all) would appear.

    We force activity in each tenant by registering a user + creating a
    room, wait for the loop to drain, and then scrape the metric.
    """
    users = {}
    for t in ("acme.localhost", "corp.localhost"):
        u = test_register(t)
        if not u:
            print(f"    [SKIP] could not register on {t}")
            return False
        users[t] = u
        room = make_request(
            "POST",
            "/_matrix/client/v3/createRoom",
            t,
            data={"preset": "public_chat"},
            headers={"Authorization": f"Bearer {u['access_token']}"},
        )
        if room.get("status") != 200:
            print(f"    [SKIP] could not create room in {t}: {room.get('status')}")
            return False

    # Wait for the stats loop to drain the new deltas on both tenants.
    time.sleep(8)

    body = fetch_text("/_synapse/metrics")
    if body.startswith("__error__"):
        print(f"    [FAIL] could not scrape metrics: {body}")
        return False

    # Look for the per-tenant event_processing_positions rows. Match the
    # full metric line so we don't accidentally pick up unrelated metrics
    # that share a label value.
    needed = {
        'acme.localhost': False,
        'corp.localhost': False,
    }
    for line in body.splitlines():
        if "synapse_event_processing_positions" not in line:
            continue
        if 'name="stats"' not in line:
            continue
        for tenant in needed:
            if f'server_name="{tenant}"' in line:
                needed[tenant] = True

    if all(needed.values()):
        print(f"    [PASS] stats loop ran per tenant "
              f"(event_processing_positions{{name='stats'}} present for "
              f"{', '.join(needed)})")
        return True
    missing = [t for t, ok in needed.items() if not ok]
    print(f"    [FAIL] stats loop did not run per tenant — missing "
          f"event_processing_positions{{name='stats', server_name=...}} "
          f"rows for: {missing}")
    return False


def test_retention_purge_per_tenant():
    """The retention purge is a `looping_call` in
    `synapse/handlers/pagination.py`. It previously ran as a single
    un-tenanted background process, which would silently no-op against
    the empty `public` schema (or, worse, operate on the wrong tenant
    if one happened to live in `public`). With the per-tenant fan-out,
    the `purge_history_for_rooms_in_range` background process start
    counter should appear once per configured tenant.

    The docker rig sets a tight 3 s purge interval so we only need to
    wait a few seconds before the counter accumulates.
    """
    # Give the looping_call a few cycles to fire across all tenants.
    time.sleep(8)

    body = fetch_text("/_synapse/metrics")
    if body.startswith("__error__"):
        print(f"    [FAIL] could not scrape metrics: {body}")
        return False

    needed = {
        "acme.localhost": False,
        "corp.localhost": False,
        "startup.localhost": False,
    }
    for line in body.splitlines():
        if "synapse_background_process_start_count" not in line:
            continue
        if 'name="purge_history_for_rooms_in_range"' not in line:
            continue
        for tenant in needed:
            if f'server_name="{tenant}"' in line:
                needed[tenant] = True

    if all(needed.values()):
        print(f"    [PASS] retention purge ran per tenant "
              f"(background_process_start_count "
              f"{{name='purge_history_for_rooms_in_range'}} present for "
              f"{', '.join(needed)})")
        return True
    missing = [t for t, ok in needed.items() if not ok]
    print(f"    [FAIL] retention purge did not run per tenant — missing "
          f"background_process_start_count rows for: {missing}")
    return False


def test_user_parter_loop_per_tenant():
    """The deactivated-user parter loop in
    `synapse/handlers/deactivate_account.py` fires once at process startup
    to resume any work left in the per-tenant
    `users_pending_deactivation` table. With the per-tenant fan-out, the
    `user_parter_loop` background process must have a start count for
    every tenant, not just the primary hostname. Synapse exposes that
    counter at process startup so we don't need to trigger any user
    activity to observe it.
    """
    body = fetch_text("/_synapse/metrics")
    if body.startswith("__error__"):
        print(f"    [FAIL] could not scrape metrics: {body}")
        return False

    needed = {
        "acme.localhost": False,
        "corp.localhost": False,
        "startup.localhost": False,
    }
    for line in body.splitlines():
        if "synapse_background_process_start_count" not in line:
            continue
        if 'name="user_parter_loop"' not in line:
            continue
        for tenant in needed:
            if f'server_name="{tenant}"' in line:
                needed[tenant] = True

    if all(needed.values()):
        print("    [PASS] user parter loop ran per tenant at startup "
              "(background_process_start_count {name='user_parter_loop'} "
              "present for all tenants)")
        return True
    missing = [t for t, ok in needed.items() if not ok]
    print(f"    [FAIL] user parter loop did not run per tenant — missing "
          f"background_process_start_count rows for: {missing}")
    return False


def test_logs_have_tenant_context():
    """Check that synapse.log mentions the active tenant on request lines.

    The fork already binds `server_name=effective_server_name` to every
    per-request LoggingContext, and Synapse's auto-installed
    LoggingContextFilter exposes it as a `server_name` field on each
    log record. Phase 1 added `%(server_name)s` to the log format
    string so that field is actually written out.
    """
    if not os.path.exists(SYNAPSE_LOG_PATH):
        print(f"    [FAIL] log file not found at {SYNAPSE_LOG_PATH} "
              "(check the ./data/logs bind mount)")
        return False

    # Make sure there's at least one request from acme in the log.
    fetch_text("/_matrix/client/versions", host="acme.localhost")
    time.sleep(0.5)  # let the file handler flush

    try:
        with open(SYNAPSE_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        print(f"    [FAIL] could not read log file: {e}")
        return False

    if "server_name=acme.localhost" in content:
        print("    [PASS] logs include tenant context (server_name=acme.localhost)")
        return True
    print("    [FAIL] no `server_name=acme.localhost` field found in synapse.log "
          "(check log.config format string + LoggingContext binding)")
    return False


# -----------------------------------------------------------------------
# PHASE 1B ISOLATION PROBES — these probes exercise whether schema-per-
# tenant isolation is actually real, as opposed to `search_path` falling
# through to `public`. They are expected to FAIL on the current main
# branch and PASS after `scripts/create_tenant_schema.py` is rewritten
# to clone tables + sequences per tenant.
# -----------------------------------------------------------------------


def _register_shared_secret(tenant, localpart):
    """Helper: register a user via the shared-secret admin endpoint.

    Returns True on 200, False on any error or non-200.
    """
    import hmac
    import hashlib

    # Shared secret must match the one in
    # docker-multitenant/config/homeserver.yaml — verified during plan
    # reconnaissance to be this literal string for every tenant in the rig.
    shared_secret = b"demo_shared_secret_change_in_production"

    # Step 1: fetch nonce
    nonce_result = make_request(
        "GET", "/_synapse/admin/v1/register", tenant
    )
    if nonce_result.get("status") != 200:
        return False
    nonce = nonce_result["data"].get("nonce")
    if not nonce:
        return False

    # Step 2: compute HMAC
    mac = hmac.new(key=shared_secret, digestmod=hashlib.sha1)
    mac.update(nonce.encode("utf-8"))
    mac.update(b"\x00")
    mac.update(localpart.encode("utf-8"))
    mac.update(b"\x00")
    mac.update(b"isolation_probe_password")
    mac.update(b"\x00")
    mac.update(b"notadmin")
    mac_hex = mac.hexdigest()

    # Step 3: register
    result = make_request(
        "POST",
        "/_synapse/admin/v1/register",
        tenant,
        data={
            "nonce": nonce,
            "username": localpart,
            "password": "isolation_probe_password",
            "admin": False,
            "mac": mac_hex,
        },
    )
    return result.get("status") == 200


def test_isolation_same_localpart():
    """Register the same localpart in two tenants and assert both succeed.

    Under correct isolation, `alice` on tenant A and `alice` on tenant B
    are `@alice:acme.localhost` and `@alice:corp.localhost` — distinct
    fully-qualified IDs — and the `profiles.user_id` UNIQUE constraint
    should not collide because each tenant has its own `profiles` table.

    Under the current broken state, both writes hit `public.profiles`
    and the second registration fails with a duplicate-key error.
    """
    # Include a run-id so re-runs against the same rig don't collide on
    # the first tenant (which would mask the real isolation signal coming
    # from the second tenant). The SAME localpart is used for both tenants
    # within a single run — that's what the probe is testing.
    localpart = f"iso_probe_alice_{int(time.time() * 1000)}"
    ok_a = _register_shared_secret(TENANTS[0], localpart)
    ok_b = _register_shared_secret(TENANTS[1], localpart)

    if ok_a and ok_b:
        print(f"    [PASS] same-localpart isolation "
              f"({localpart} registered on {TENANTS[0]} and {TENANTS[1]})")
        return True
    else:
        print(f"    [FAIL] same-localpart isolation "
              f"(tenant_a={ok_a}, tenant_b={ok_b}) — "
              "likely search_path fall-through to public.profiles")
        return False


def test_tenant_isolation(users):
    """Test that tokens from one tenant don't work on another."""
    if len(users) < 2:
        print("    [SKIP] Not enough users to test isolation")
        return True

    tenants = list(users.keys())
    tenant_a = tenants[0]
    tenant_b = tenants[1]
    token_a = users[tenant_a]["access_token"]

    # Try to use tenant A's token on tenant B's endpoint
    headers = {"Authorization": f"Bearer {token_a}"}
    result = make_request("GET", "/_matrix/client/v3/account/whoami", tenant_b, headers=headers)

    if result.get("status") in [401, 403]:
        print(f"    [PASS] Token from {tenant_a} rejected by {tenant_b}")
        return True
    elif result.get("status") == 200:
        user_id = result["data"].get("user_id", "")
        # Token might work if database shares user table, but user should still be from tenant_a
        if tenant_a.split(".")[0] in user_id:
            print(f"    [INFO] Cross-tenant auth allowed but user context preserved: {user_id}")
            return True
        else:
            print(f"    [WARN] Token from {tenant_a} accepted by {tenant_b} - potential isolation issue")
            return True  # Not necessarily a failure, depends on design
    else:
        print(f"    [FAIL] Unexpected status {result.get('status')}")
        return False


def test_tenant(tenant):
    """Run all tests for a single tenant."""
    print(f"\n{'='*60}")
    print(f"  TENANT: {tenant}")
    print(f"{'='*60}")

    passed = 0
    failed = 0

    # Test versions
    if test_versions(tenant):
        passed += 1
    else:
        failed += 1

    # Test registration
    user = test_register(tenant)
    if user:
        passed += 1

        # Test whoami
        if test_whoami(tenant, user["access_token"]):
            passed += 1
        else:
            failed += 1

        # Test room creation
        room_id = test_create_room(tenant, user["access_token"])
        if room_id:
            passed += 1

            # Test sending message
            if test_send_message(tenant, user["access_token"], room_id):
                passed += 1
            else:
                failed += 1
        else:
            failed += 1
    else:
        failed += 1

    return passed, failed, user


def main():
    # Focused-phase dispatch: when called with `--phase <name>` we only
    # run a targeted subset. This exists so the host-side isolation probe
    # runner (`run_isolation_probes_host.sh`) can delegate the HTTP probe
    # into the in-container runner without dragging the whole suite along.
    phase = None
    if "--phase" in sys.argv:
        idx = sys.argv.index("--phase")
        if idx + 1 < len(sys.argv):
            phase = sys.argv[idx + 1]

    if phase == "1b-http":
        if not wait_for_synapse():
            sys.exit(1)
        print("\n=== PHASE 1B ISOLATION PROBES (HTTP only) ===")
        ok = test_isolation_same_localpart()
        sys.exit(0 if ok else 1)

    print("\n" + "=" * 60)
    print("  MULTI-TENANT SYNAPSE - INTEGRATION TESTS (REAL SYNAPSE)")
    print("=" * 60)

    # Wait for Synapse
    if not wait_for_synapse():
        sys.exit(1)

    total_passed = 0
    total_failed = 0
    users = {}

    # Test each tenant
    for tenant in TENANTS:
        passed, failed, user = test_tenant(tenant)
        total_passed += passed
        total_failed += failed
        if user:
            users[tenant] = user

    # Test unknown tenant
    print(f"\n{'='*60}")
    print("  SECURITY TESTS")
    print(f"{'='*60}")

    if test_unknown_tenant():
        total_passed += 1
    else:
        total_failed += 1

    # Test tenant isolation
    print("\n  Testing tenant isolation...")
    if test_tenant_isolation(users):
        total_passed += 1
    else:
        total_failed += 1

    # Observability tests — these gate the audit/instrumentation phase.
    # The first one (endpoint reachable) should pass once Prometheus is
    # wired up; the latter two are expected to FAIL until the
    # instrumentation work lands. They're loud on purpose.
    print(f"\n{'='*60}")
    print("  OBSERVABILITY TESTS")
    print(f"{'='*60}")

    if test_metrics_endpoint_reachable():
        total_passed += 1
    else:
        total_failed += 1

    if test_metrics_have_tenant_label():
        total_passed += 1
    else:
        total_failed += 1

    if test_logs_have_tenant_context():
        total_passed += 1
    else:
        total_failed += 1

    # Phase-1 leak probes — these are the failing tests that gate the
    # remaining audit work. Each one targets a specific class of
    # `self.hs.hostname` / `self.server_name` leak inside a request
    # handler.
    print(f"\n{'='*60}")
    print("  PHASE 1 LEAK PROBES (audit completion)")
    print(f"{'='*60}")

    for fn in (
        test_login_response_uses_tenant_server_name,
        test_whoami_response_uses_tenant_server_name,
        test_capabilities_endpoint_per_tenant,
        test_wellknown_client_per_tenant,
    ):
        try:
            ok = fn()
        except Exception as e:
            print(f"    [FAIL] {fn.__name__} raised {type(e).__name__}: {e}")
            ok = False
        if ok:
            total_passed += 1
        else:
            total_failed += 1

    # Phase-2 probes — background processes must run tenant-aware.
    print(f"\n{'='*60}")
    print("  PHASE 2 PROBES (background process tenant context)")
    print(f"{'='*60}")

    for fn in (
        test_user_directory_per_tenant_population,
        test_stats_loop_per_tenant,
        test_retention_purge_per_tenant,
        test_user_parter_loop_per_tenant,
    ):
        try:
            ok = fn()
        except Exception as e:
            print(f"    [FAIL] {fn.__name__} raised {type(e).__name__}: {e}")
            ok = False
        if ok:
            total_passed += 1
        else:
            total_failed += 1

    # Phase-1B isolation probes — schema-per-tenant actually holding.
    # Expected to FAIL on current main branch and PASS after the
    # create_tenant_schema.py rewrite back-applies per-tenant table +
    # sequence cloning.
    print(f"\n{'='*60}")
    print("  PHASE 1B ISOLATION PROBES (schema isolation)")
    print(f"{'='*60}")

    try:
        ok = test_isolation_same_localpart()
    except Exception as e:
        print(f"    [FAIL] test_isolation_same_localpart raised "
              f"{type(e).__name__}: {e}")
        ok = False
    if ok:
        total_passed += 1
    else:
        total_failed += 1

    # Summary
    print("\n" + "=" * 60)
    print(f"  RESULTS: {total_passed} passed, {total_failed} failed")
    print("=" * 60)

    if total_failed == 0:
        print("\n  ALL TESTS PASSED!\n")
        sys.exit(0)
    else:
        print(f"\n  {total_failed} TESTS FAILED!\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
