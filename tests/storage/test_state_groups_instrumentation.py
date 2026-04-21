#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
#
"""F#2 state-groups instrumentation: env-gate assertion tests.

Task 6 of the stress-test-findings-fix plan instrumented every write to
``state_groups`` / ``state_group_edges`` / ``state_groups_persisting`` with a
guard helper (``_mt_state_groups_guard``). When
``SYNAPSE_MT_STRICT_STATE_GROUPS=1`` is set in the environment, the guard
raises ``AssertionError`` if it fires with no active tenant context. This
module proves the env gate works in both directions (raises when set, silent
when not).
"""

import os
from unittest import mock

from twisted.trial.unittest import SynchronousTestCase


class StrictModeEnvGateTestCase(SynchronousTestCase):
    """F#2 instrumentation — env-gated AssertionError on unset tenant writes."""

    def test_strict_mode_raises_with_no_tenant_context(self) -> None:
        """With the env var set and no tenant in context, the guard raises."""
        from synapse.storage.databases.state.store import _mt_state_groups_guard

        mock_txn = mock.MagicMock()
        mock_txn.execute.return_value = None
        mock_txn.fetchone.side_effect = [
            ("public",),  # current_setting('search_path') response
            ("public",),  # pg_class schema lookup response
        ]

        with mock.patch.dict(
            os.environ, {"SYNAPSE_MT_STRICT_STATE_GROUPS": "1"}, clear=False
        ):
            self.assertRaises(
                AssertionError,
                _mt_state_groups_guard,
                mock_txn,
                "test_op",
            )

    def test_strict_mode_off_is_silent(self) -> None:
        """With the env var unset, the guard must not raise even if tenant is None."""
        from synapse.storage.databases.state.store import _mt_state_groups_guard

        mock_txn = mock.MagicMock()
        mock_txn.execute.return_value = None
        mock_txn.fetchone.side_effect = [("public",), ("public",)]

        # Ensure env var is NOT set (wipe inherited env just in case)
        env = {
            k: v
            for k, v in os.environ.items()
            if k != "SYNAPSE_MT_STRICT_STATE_GROUPS"
        }
        with mock.patch.dict(os.environ, env, clear=True):
            _mt_state_groups_guard(mock_txn, "test_op")  # must not raise

    def test_helper_handles_none_schema_lookup(self) -> None:
        """If the pg_class lookup returns None (e.g. SQLite in unit tests) the
        helper still emits the log line without raising, as long as strict mode
        is off.
        """
        from synapse.storage.databases.state.store import _mt_state_groups_guard

        mock_txn = mock.MagicMock()
        mock_txn.execute.return_value = None
        mock_txn.fetchone.side_effect = [("public",), None]  # pg_class returns nothing

        env = {
            k: v
            for k, v in os.environ.items()
            if k != "SYNAPSE_MT_STRICT_STATE_GROUPS"
        }
        with mock.patch.dict(os.environ, env, clear=True):
            _mt_state_groups_guard(mock_txn, "test_op")  # must not raise
