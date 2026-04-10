#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

"""
Red probes for phase 9a: tenant-keyed federation sender queues.

Probes verify that:
1. PerDestinationQueue stores tenant server_name when tenant_server_name is passed
2. Two tenants sending to the same destination get separate queues
3. _tenant_for_event extracts tenant server_name from event sender
4. TransactionManager.send_new_transaction accepts an origin parameter
"""

from unittest import TestCase
from unittest.mock import MagicMock

from synapse.config.tenants import TenantConfig
from synapse.federation.sender import FederationSender
from synapse.federation.sender.per_destination_queue import PerDestinationQueue
from synapse.federation.sender.transaction_manager import TransactionManager


def _make_tenant(name: str) -> TenantConfig:
    return TenantConfig(
        server_name=f"{name}.localhost",
        database_schema=f"tenant_{name}",
        signing_key_path=f"/keys/{name}.key",
        media_store_path=f"/media/{name}",
    )


def _mock_hs(hostname: str = "main.localhost") -> MagicMock:
    """Minimal HomeServer mock for PerDestinationQueue construction."""
    hs = MagicMock()
    hs.hostname = hostname
    hs.get_clock.return_value = MagicMock()
    hs.get_storage_controllers.return_value = MagicMock()
    hs.get_datastores.return_value.main = MagicMock()
    hs.get_instance_name.return_value = "master"
    hs.config.worker.federation_shard_config.should_handle.return_value = True
    return hs


# ── Probe 1: PerDestinationQueue stores tenant server_name ───────

class TestPerDestinationQueueTenantServerName(TestCase):
    """When tenant_server_name is passed, the queue's server_name must
    reflect the tenant, not hs.hostname."""

    def test_server_name_from_tenant(self) -> None:
        tenant = _make_tenant("acme")
        hs = _mock_hs("main.localhost")
        tm = MagicMock()

        queue = PerDestinationQueue(
            hs=hs,
            transaction_manager=tm,
            destination="remote.example.com",
            tenant_server_name=tenant.server_name,
        )
        self.assertEqual(queue.server_name, "acme.localhost")

    def test_server_name_default_without_tenant(self) -> None:
        hs = _mock_hs("main.localhost")
        tm = MagicMock()

        queue = PerDestinationQueue(
            hs=hs,
            transaction_manager=tm,
            destination="remote.example.com",
        )
        self.assertEqual(queue.server_name, "main.localhost")

    def test_tenant_signing_key_stored(self) -> None:
        tenant = _make_tenant("acme")
        hs = _mock_hs("main.localhost")
        tm = MagicMock()
        fake_key = MagicMock()

        queue = PerDestinationQueue(
            hs=hs,
            transaction_manager=tm,
            destination="remote.example.com",
            tenant_server_name=tenant.server_name,
            tenant_signing_key=fake_key,
        )
        self.assertIs(queue._tenant_signing_key, fake_key)


# ── Probe 2: Separate queues per tenant ──────────────────────────

class TestSeparateQueuesPerTenant(TestCase):
    """FederationSender._get_per_destination_queue(tenant, dest) must
    return distinct queues for different tenants targeting the same
    destination."""

    def test_two_tenants_get_separate_queues(self) -> None:
        from synapse.federation.sender import FederationSender

        hs = _mock_hs("main.localhost")
        # This probe expects _get_per_destination_queue to accept
        # (tenant_server_name, destination) — two positional args.
        # It will fail until the FederationSender is updated.
        sender = MagicMock(spec=FederationSender)
        sender.hs = hs
        sender._per_destination_queues = {}
        sender._transaction_manager = MagicMock()

        # If the method signature only takes destination, this call will
        # raise TypeError — which is the expected red probe failure.
        try:
            q1 = FederationSender._get_per_destination_queue(
                sender, "acme.localhost", "remote.example.com"
            )
            q2 = FederationSender._get_per_destination_queue(
                sender, "corp.localhost", "remote.example.com"
            )
            self.assertIsNot(q1, q2)
        except TypeError:
            self.fail(
                "_get_per_destination_queue does not accept "
                "(tenant_server_name, destination) yet"
            )


# ── Probe 3: _tenant_for_event ───────────────────────────────────

class TestTenantForEvent(TestCase):
    """FederationSender._tenant_for_event(event) must extract the tenant
    server_name from the event sender's domain."""

    def test_extracts_tenant_from_sender(self) -> None:
        from synapse.federation.sender import FederationSender

        hs = _mock_hs("main.localhost")
        hs.is_mine_server_name.return_value = True
        sender = MagicMock(spec=FederationSender)
        sender.hs = hs
        event = MagicMock()
        event.sender = "@alice:acme.localhost"

        try:
            result = FederationSender._tenant_for_event(sender, event)
            self.assertEqual(result, "acme.localhost")
        except AttributeError:
            self.fail("_tenant_for_event does not exist on FederationSender yet")


# ── Probe 4: TransactionManager.send_new_transaction origin param ─

class TestTransactionManagerOriginParam(TestCase):
    """send_new_transaction must accept an explicit origin parameter so
    the PerDestinationQueue can pass the tenant server_name."""

    def test_send_new_transaction_has_origin_param(self) -> None:
        import inspect

        sig = inspect.signature(TransactionManager.send_new_transaction)
        self.assertIn(
            "origin",
            sig.parameters,
            "send_new_transaction is missing the 'origin' parameter",
        )


# ── 9b probes: tenant-aware signing ──────────────────────────────

from signedjson import key as signing_key_mod


class TestBuildAuthHeadersTenantOverride(TestCase):
    """build_auth_headers must use tenant origin + signing_key when provided."""

    def test_auth_header_uses_tenant_origin(self) -> None:
        from synapse.http.matrixfederationclient import MatrixFederationHttpClient

        client = MagicMock(spec=MatrixFederationHttpClient)

        # Generate a real signing key for acme tenant
        tenant_key = signing_key_mod.generate_signing_key("acme0")
        client.server_name = "main.localhost"
        client.signing_key = signing_key_mod.generate_signing_key("main0")

        # Call the real build_auth_headers with tenant overrides
        headers = MatrixFederationHttpClient.build_auth_headers(
            client,
            destination=b"matrix.org",
            method=b"PUT",
            url_bytes=b"/_matrix/federation/v1/send/123",
            content={"pdus": [], "edus": []},
            origin="acme.localhost",
            signing_key=tenant_key,
        )

        self.assertTrue(len(headers) > 0)
        header_str = headers[0].decode("ascii") if isinstance(headers[0], bytes) else headers[0]
        self.assertIn('origin="acme.localhost"', header_str)
        self.assertNotIn('origin="main.localhost"', header_str)


class TestBuildAuthHeadersFallback(TestCase):
    """build_auth_headers without overrides must use self.server_name."""

    def test_auth_header_fallback(self) -> None:
        from synapse.http.matrixfederationclient import MatrixFederationHttpClient

        client = MagicMock(spec=MatrixFederationHttpClient)
        client.server_name = "main.localhost"
        client.signing_key = signing_key_mod.generate_signing_key("main0")

        headers = MatrixFederationHttpClient.build_auth_headers(
            client,
            destination=b"matrix.org",
            method=b"PUT",
            url_bytes=b"/_matrix/federation/v1/send/123",
            content={"pdus": [], "edus": []},
        )

        self.assertTrue(len(headers) > 0)
        header_str = headers[0].decode("ascii") if isinstance(headers[0], bytes) else headers[0]
        self.assertIn('origin="main.localhost"', header_str)


# ── 9c probes: event loop tenant dispatch ────────────────────────


class TestSendPduAcceptsTenant(TestCase):
    """_send_pdu must accept a tenant_server_name parameter."""

    def test_send_pdu_has_tenant_param(self) -> None:
        import inspect
        from synapse.federation.sender import FederationSender
        sig = inspect.signature(FederationSender._send_pdu)
        self.assertIn("tenant_server_name", sig.parameters)


class TestWakeDestinationAcceptsTenant(TestCase):
    """wake_destination must accept a tenant_server_name parameter."""

    def test_wake_destination_has_tenant_param(self) -> None:
        import inspect
        from synapse.federation.sender import FederationSender
        sig = inspect.signature(FederationSender.wake_destination)
        self.assertIn("tenant_server_name", sig.parameters)


# ── 9d probes: EDU origin stamping ───────────────────────────────


class TestEduOriginUsesTenantServerName(TestCase):
    """EDUs built by PerDestinationQueue must use tenant server_name as origin."""

    def test_receipt_edu_origin(self) -> None:
        hs = MagicMock()
        hs.hostname = "main.localhost"
        hs.get_clock.return_value = MagicMock()
        hs.get_storage_controllers.return_value = MagicMock()
        hs.get_datastores.return_value.main = MagicMock()

        tm = MagicMock(spec=TransactionManager)
        queue = PerDestinationQueue(
            hs, tm, "matrix.org", tenant_server_name="acme.localhost"
        )

        # Queue a read receipt and extract the EDU
        receipt_content = {"room_id": "!room:acme.localhost", "m.read": {}}
        queue._pending_receipt_edus.append(receipt_content)

        edus = list(queue._get_receipt_edus(limit=10))
        self.assertEqual(len(edus), 1)
        self.assertEqual(edus[0].origin, "acme.localhost")

    def test_queue_server_name_propagates_to_edus(self) -> None:
        """Verify that the queue's server_name (set to tenant) is what EDUs use."""
        hs = MagicMock()
        hs.hostname = "main.localhost"
        hs.get_clock.return_value = MagicMock()
        hs.get_storage_controllers.return_value = MagicMock()
        hs.get_datastores.return_value.main = MagicMock()

        tm = MagicMock(spec=TransactionManager)
        queue = PerDestinationQueue(
            hs, tm, "matrix.org", tenant_server_name="corp.localhost"
        )

        # The key invariant: server_name on the queue IS the tenant name
        self.assertEqual(queue.server_name, "corp.localhost")
        self.assertNotEqual(queue.server_name, "main.localhost")


class TestRetryLimiterUsesTenantServerName(TestCase):
    """get_retry_limiter must be called with tenant server_name."""

    def test_retry_limiter_server_name(self) -> None:
        hs = MagicMock()
        hs.hostname = "main.localhost"
        hs.get_clock.return_value = MagicMock()
        hs.get_storage_controllers.return_value = MagicMock()
        hs.get_datastores.return_value.main = MagicMock()

        tm = MagicMock(spec=TransactionManager)
        queue = PerDestinationQueue(
            hs, tm, "matrix.org", tenant_server_name="acme.localhost"
        )

        # The retry limiter at line 351 uses self.server_name
        # which should now be the tenant's server_name
        self.assertEqual(queue.server_name, "acme.localhost")


# ── Integration probe ────────────────────────────────────────────


class TestFullSendPathPerTenant(TestCase):
    """Integration: two tenants sending to same remote get separate
    transactions with correct origins."""

    def test_two_tenants_separate_transactions(self) -> None:
        hs = MagicMock()
        hs.hostname = "main.localhost"
        hs.get_clock.return_value = MagicMock()
        hs.get_datastores.return_value.main = MagicMock()
        hs.get_storage_controllers.return_value = MagicMock()
        hs.config.federation.is_domain_allowed_according_to_federation_whitelist.return_value = True
        hs.is_mine_server_name.side_effect = lambda sn: sn in (
            "acme.localhost", "corp.localhost", "main.localhost"
        )

        sender = MagicMock(spec=FederationSender)
        sender.hs = hs
        sender._per_destination_queues = {}
        sender._transaction_manager = MagicMock(spec=TransactionManager)

        # Create queues for two tenants to same destination
        q_acme = FederationSender._get_per_destination_queue(
            sender, "acme.localhost", "matrix.org"
        )
        q_corp = FederationSender._get_per_destination_queue(
            sender, "corp.localhost", "matrix.org"
        )

        # Verify isolation
        self.assertIsNot(q_acme, q_corp)
        self.assertEqual(q_acme.server_name, "acme.localhost")
        self.assertEqual(q_corp.server_name, "corp.localhost")

        # Verify queue dict has two entries
        self.assertEqual(len(sender._per_destination_queues), 2)
        self.assertIn(("acme.localhost", "matrix.org"), sender._per_destination_queues)
        self.assertIn(("corp.localhost", "matrix.org"), sender._per_destination_queues)

        # Verify TransactionManager signature accepts origin + signing_key
        import inspect
        sig = inspect.signature(TransactionManager.send_new_transaction)
        self.assertIn("origin", sig.parameters)
        self.assertIn("signing_key", sig.parameters)

    def test_tenant_for_event_routes_correctly(self) -> None:
        """Events from different tenants are routed to correct tenant queues."""
        sender = MagicMock(spec=FederationSender)
        sender.hs = MagicMock()
        sender.hs.is_mine_server_name.side_effect = lambda sn: sn in (
            "acme.localhost", "corp.localhost",
        )

        event_acme = MagicMock()
        event_acme.sender = "@alice:acme.localhost"

        event_corp = MagicMock()
        event_corp.sender = "@bob:corp.localhost"

        event_remote = MagicMock()
        event_remote.sender = "@charlie:remote.server"

        self.assertEqual(
            FederationSender._tenant_for_event(sender, event_acme),
            "acme.localhost"
        )
        self.assertEqual(
            FederationSender._tenant_for_event(sender, event_corp),
            "corp.localhost"
        )
        self.assertIsNone(
            FederationSender._tenant_for_event(sender, event_remote)
        )
