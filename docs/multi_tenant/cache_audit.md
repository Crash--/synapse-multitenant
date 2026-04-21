# Storage Cache Multi-Tenancy Audit

**Date:** 2026-04-21
**Scope:** Every `@cached` / `@cachedList` decorator in `synapse/storage/`.
**Goal:** Identify which need to swap to `@tenant_cached` / `@tenant_cached_list` to close Finding #1 (cross-tenant cache leak).

## Background

`scripts/create_tenant_schema.py` clones **every** BASE TABLE from `public`
into each tenant schema (see `discover_schema_surface()` — it enumerates all
`information_schema.tables` rows without an exclusion list). This means
every table that upstream Synapse stores in `public` is also a per-tenant
table in tenant schemas. With `search_path` pointing at the tenant schema,
SQL transparently reads/writes the tenant's copy.

Consequence for caching: a per-process `@cached` dictionary does NOT know
which tenant's copy produced the row it memoised. If the cache key doesn't
itself carry tenant information, a lookup with the same key from a different
tenant will hit the prior tenant's cache entry → cross-tenant data leak.

## Classification rules

Applied per Task 3 heuristic in the plan:

| Class | Criterion |
|---|---|
| `swap` | Reads a tenant-scoped table AND cache key does NOT disambiguate per tenant |
| `safe-explicit` | Cache key includes data that disambiguates per tenant (fully-qualified user_id `@x:server.name`, or a Matrix `!room_id:server.name`) |
| `not-tenant-scoped` | Reads only public-only metadata (not applicable here — everything in `synapse/storage/` reads cloned tenant-scoped tables) |

Tenant-scoping qualifier heuristics:
- Bare `token`, bare `device_id`, bare `event_id`, bare `state_group`, bare integer PK, opaque hash: **NOT** tenant-qualified → `swap`.
- Fully-qualified Matrix user_id `@user:server.name`: qualified → `safe-explicit`.
- Matrix room_id `!opaque:server.name`: accepted as qualified per plan heuristic → `safe-explicit` (see "Known gaps" below).
- Zero positional args (`num_args=0`, or only stream-token args shared across tenants): NOT tenant-qualified → `swap`.
- Remote server name (e.g. `destination`, `server_name` for federated remotes): identifies the remote, not the tenant observing it → `swap`.

No `not-tenant-scoped` entries were found in this audit; `public.tenants` and
`public.tenant_keys` (the tenant-metadata tables added by this fork) are NOT
read by any `@cached` method in `synapse/storage/`. Every cached method in
scope reads at least one table that is cloned into each tenant schema.

## Classification

| File:Line | Method | Table(s) | Key args | Class | Reasoning |
|---|---|---|---|---|---|
| `synapse/storage/databases/main/directory.py:83` | `get_aliases_for_room` | `room_aliases` | `room_id` | `safe-explicit` | room_id qualified. |
| `synapse/storage/databases/main/tags.py:40` | `get_tags_for_user` | `room_tags` | `user_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/tags.py:207` | `get_tags_for_room` | `room_tags` | `user_id, room_id` | `safe-explicit` | Both qualified. |
| `synapse/storage/databases/main/event_push_actions.py:496` | `get_unread_event_push_actions_by_room_for_user` | `event_push_actions` (via txn) | `room_id, user_id` | `safe-explicit` | Both qualified. |
| `synapse/storage/databases/main/appservice.py:199` | `get_app_service_users_in_room` | via `get_local_users_in_room` | `room_id, app_service, cache_context` | `safe-explicit` | room_id qualified; app_service adds further discrimination. |
| `synapse/storage/databases/main/receipts.py:268` | `_get_receipts_for_user_with_orderings` | `receipts_linearized`, `events` | `user_id, receipt_type` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/receipts.py:368` | `_get_linearized_receipts_for_room` | `receipts_linearized` | `room_id, to_key, from_key` | `safe-explicit` | room_id qualified. |
| `synapse/storage/databases/main/receipts.py:418` | `_get_linearized_receipts_for_rooms` (@cachedList) | `receipts_linearized` | list on `room_ids`, `to_key`, `from_key` | `safe-explicit` | Keyed per room_id. |
| `synapse/storage/databases/main/receipts.py:574` | `get_linearized_receipts_for_all_rooms` | `receipts_linearized` | `to_key, from_key` (stream tokens only) | `swap` | No tenant-scoped key — per-tenant sequences mean tokens of the same int value map to different tenants' streams. verify-in-Task-5 |
| `synapse/storage/databases/main/filtering.py:152` | `get_user_filter` | `user_filters` | `user_id (UserID), filter_id` | `safe-explicit` | UserID.to_string() is full mxid. |
| `synapse/storage/databases/main/stream.py:1740` | `_get_max_event_pos` | `events` (via @cachedList impl) | `room_id` | `safe-explicit` | room_id qualified. |
| `synapse/storage/databases/main/stream.py:1744` | `_bulk_get_max_event_pos` (@cachedList) | `events` | list on `room_ids` | `safe-explicit` | Keyed per room_id. |
| `synapse/storage/databases/main/stream.py:2483` | `get_id_for_instance` | `instance_map` | `instance_name` | `swap` | `instance_name` (e.g. `master`) is not tenant-scoped; each tenant has its own `instance_map` rows that may assign different ids. verify-in-Task-5 |
| `synapse/storage/databases/main/stream.py:2521` | `get_name_from_instance_id` | `instance_map` | `instance_id (int)` | `swap` | Bare integer PK; per-tenant `instance_map` can map the same id to different names. |
| `synapse/storage/databases/main/stats.py:294` | `get_earliest_token_for_stats` | `<stats_type>_current` (room_stats_current, user_stats_current) | `stats_type, id` | `safe-explicit` | `id` is either a room_id or a full user_id depending on stats_type. |
| `synapse/storage/databases/main/user_erasure_store.py:29` | `is_user_erased` | `erased_users` | `user_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/user_erasure_store.py:48` | `are_users_erased` (@cachedList) | `erased_users` | list on `user_ids` | `safe-explicit` | Keyed per full mxid. |
| `synapse/storage/databases/main/transactions.py:169` | `get_destination_retry_timings` | `destinations` | `destination` | `swap` | `destination` is a remote server name (e.g. `matrix.org`); two tenants share the string but have independent retry state. verify-in-Task-5 |
| `synapse/storage/databases/main/transactions.py:212` | `get_destination_retry_timings_batch` (@cachedList) | `destinations` | list on `destinations` | `swap` | Same as above. |
| `synapse/storage/databases/main/monthly_active_users.py:79` | `get_monthly_active_count` | `monthly_active_users`, `users` | `num_args=0` (none) | `swap` | Zero-arg cache — global singleton; reads tenant `users` table. Catastrophic leak. |
| `synapse/storage/databases/main/monthly_active_users.py:102` | `get_monthly_active_count_by_service` | `monthly_active_users`, `users` | `num_args=0` (none) | `swap` | Zero-arg cache. |
| `synapse/storage/databases/main/monthly_active_users.py:196` | `user_last_seen_monthly_active` | `monthly_active_users` | `user_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/account_data.py:125` | `get_global_account_data_for_user` | `account_data` | `user_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/account_data.py:170` | `get_room_account_data_for_user` | `room_account_data` | `user_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/account_data.py:218` | `get_global_account_data_by_type_for_user` | `account_data` | `user_id, data_type` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/account_data.py:270` | `get_account_data_for_room` | `room_account_data` | `user_id, room_id` | `safe-explicit` | Both qualified. |
| `synapse/storage/databases/main/account_data.py:305` | `get_account_data_for_room_and_type` | `room_account_data` | `user_id, room_id, account_data_type` | `safe-explicit` | Both qualified. |
| `synapse/storage/databases/main/account_data.py:529` | `ignored_by` | `ignored_users` | `user_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/account_data.py:549` | `ignored_users` | `ignored_users` | `user_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/push_rule.py:201` | `get_push_rules_for_user` | `push_rules`, `push_rules_enable` | `user_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/push_rule.py:262` | `bulk_get_push_rules` (@cachedList) | same | list on `user_ids` | `safe-explicit` | Keyed per full mxid. |
| `synapse/storage/databases/main/room.py:575` | `is_room_blocked` | `blocked_rooms` | `room_id` | `safe-explicit` | room_id qualified. |
| `synapse/storage/databases/main/room.py:800` | `get_ratelimit_for_user` | `ratelimit_override` | `user_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/room.py:880` | `get_retention_policy_for_room` | `room_retention`, `current_state_events` | `room_id` | `safe-explicit` | room_id qualified. |
| `synapse/storage/databases/main/room.py:1438` | `_get_partial_state_servers_at_join` | `partial_state_rooms_servers` | `room_id` | `safe-explicit` | room_id qualified. |
| `synapse/storage/databases/main/room.py:1499` | `is_partial_state_room` | `partial_state_rooms` | `room_id` | `safe-explicit` | room_id qualified. |
| `synapse/storage/databases/main/room.py:1518` | `is_partial_state_room_batched` (@cachedList) | `partial_state_rooms` | list on `room_ids` | `safe-explicit` | Keyed per room_id. |
| `synapse/storage/databases/main/room.py:1542` | `get_partial_rooms` | `partial_state_rooms` | none (zero args) | `swap` | Zero-arg cache returning set of room_ids from tenant schema — data differs per tenant. |
| `synapse/storage/databases/main/sliding_sync.py:388` | `get_and_clear_connection_positions` | `sliding_sync_connections` etc | `user_id, device_id, conn_id, connection_position` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/keys.py:131` | `_get_server_keys_json` | `server_keys_json` | `(server_name, key_id)` tuple | `swap` | Key is remote `(server_name, key_id)`; not tenant-qualified. `server_keys_json` is cloned per-tenant and tenants may cache different fetched versions of the same remote server key. verify-in-Task-5 |
| `synapse/storage/databases/main/keys.py:137` | `get_server_keys_json` (@cachedList) | `server_keys_json` | list on `server_name_and_key_ids` | `swap` | Same. |
| `synapse/storage/databases/main/keys.py:199` | `get_server_key_json_for_remote` | `server_keys_json` | `server_name, key_id` | `swap` | Same. |
| `synapse/storage/databases/main/keys.py:207` | `get_server_keys_json_for_remote` (@cachedList) | `server_keys_json` | `server_name`, list on `key_ids` | `swap` | Same. |
| `synapse/storage/databases/main/roommember.py:161` | `get_users_in_room` | `current_state_events` | `room_id` | `safe-explicit` | room_id qualified. |
| `synapse/storage/databases/main/roommember.py:211` | `get_user_in_room_with_profile` | (impl via @cachedList) | `room_id, user_id` | `safe-explicit` | Both qualified. |
| `synapse/storage/databases/main/roommember.py:215` | `get_subset_users_in_room_with_profiles` (@cachedList) | `room_memberships`, `current_state_events` | `room_id`, list on `user_ids` | `safe-explicit` | Keyed per (room_id, user_id). |
| `synapse/storage/databases/main/roommember.py:261` | `get_users_in_room_with_profiles` | `room_memberships`, `current_state_events` | `room_id` | `safe-explicit` | room_id qualified. |
| `synapse/storage/databases/main/roommember.py:302` | `get_room_summary` | `current_state_events` | `room_id` | `safe-explicit` | room_id qualified. |
| `synapse/storage/databases/main/roommember.py:379` | `get_member_counts` | `current_state_events` | `room_id` | `safe-explicit` | room_id qualified. |
| `synapse/storage/databases/main/roommember.py:404` | `get_number_joined_users_in_room` | `current_state_events` | `room_id` | `safe-explicit` | room_id qualified. |
| `synapse/storage/databases/main/roommember.py:413` | `get_invited_rooms_for_local_user` | (delegates) | `user_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/roommember.py:511` | `_get_rooms_for_local_user_where_membership_is_inner` | `local_current_membership`, `events`, `rooms` | `user_id, membership_list` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/roommember.py:586` | `get_local_users_in_room` | `local_current_membership` | `room_id` | `safe-explicit` | room_id qualified. |
| `synapse/storage/databases/main/roommember.py:771` | `get_rooms_for_user` | `current_state_events` | `user_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/roommember.py:792` | `_get_rooms_for_users` (@cachedList) | `current_state_events` | list on `user_ids` | `safe-explicit` | Keyed per full mxid. |
| `synapse/storage/databases/main/roommember.py:845` | `does_pair_of_users_share_a_room` | (impl via @cachedList) | `user_id, other_user_id` | `safe-explicit` | Both full mxids. |
| `synapse/storage/databases/main/roommember.py:851` | `_do_users_share_a_room` (@cachedList) | `current_state_events` | `user_id`, list on `other_user_ids` | `safe-explicit` | Full mxids. |
| `synapse/storage/databases/main/roommember.py:907` | `does_pair_of_users_share_a_room_joined_or_invited` | (impl via @cachedList) | `user_id, other_user_id` | `safe-explicit` | Both full mxids. |
| `synapse/storage/databases/main/roommember.py:913` | `_do_users_share_a_room_joined_or_invited` (@cachedList) | `current_state_events` | `user_id`, list on `other_user_ids` | `safe-explicit` | Full mxids. |
| `synapse/storage/databases/main/roommember.py:985` | `get_mutual_rooms_between_users` | via `get_rooms_for_user` | `user_ids: frozenset, cache_context` | `safe-explicit` | frozenset of full mxids. |
| `synapse/storage/databases/main/roommember.py:1060` | `_get_user_id_from_membership_event_id` | `room_memberships` | `event_id` | `swap` | Opaque event_id; conservative per plan heuristic. verify-in-Task-5 |
| `synapse/storage/databases/main/roommember.py:1071` | `_get_user_ids_from_membership_event_ids` (@cachedList) | `room_memberships` | list on `event_ids` | `swap` | Same. |
| `synapse/storage/databases/main/roommember.py:1103` | `is_host_joined` | `current_state_events` | `room_id, host` | `safe-explicit` | room_id qualified. |
| `synapse/storage/databases/main/roommember.py:1107` | `is_host_invited` | `current_state_events` | `room_id, host` | `safe-explicit` | room_id qualified. |
| `synapse/storage/databases/main/roommember.py:1145` | `get_current_hosts_in_room` | `current_state_events` | `room_id` | `safe-explicit` | room_id qualified. |
| `synapse/storage/databases/main/roommember.py:1182` | `get_current_hosts_in_room_ordered` | `current_state_events`, `events` | `room_id` | `safe-explicit` | room_id qualified. |
| `synapse/storage/databases/main/roommember.py:1274` | `_get_joined_hosts_cache` | (in-memory structure keyed by room_id) | `room_id` | `safe-explicit` | room_id qualified. |
| `synapse/storage/databases/main/roommember.py:1278` | `did_forget` | `room_memberships` | `user_id, room_id` | `safe-explicit` | Both qualified. |
| `synapse/storage/databases/main/roommember.py:1304` | `get_forgotten_rooms_for_user` | `room_memberships` | `user_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/roommember.py:1407` | `_get_membership_from_event_id` | `room_memberships` | `member_event_id` | `swap` | Opaque event_id; conservative. verify-in-Task-5 |
| `synapse/storage/databases/main/roommember.py:1413` | `get_membership_from_event_ids` (@cachedList) | `room_memberships` | list on `member_event_ids` | `swap` | Same. |
| `synapse/storage/databases/main/roommember.py:1531` | `get_sliding_sync_rooms_for_user_from_membership_snapshots` | `sliding_sync_membership_snapshots` | `user_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/devices.py:469` | `get_device` | `devices` | `user_id, device_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/devices.py:1038` | `_get_cached_user_device` | `device_lists_remote_cache` | `user_id, device_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/devices.py:1050` | `get_cached_devices_for_user` | `device_lists_remote_cache` | `user_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/devices.py:1298` | `get_device_list_last_stream_id_for_remote` | `device_lists_remote_extremeties` | `user_id` | `safe-explicit` | Full remote mxid. |
| `synapse/storage/databases/main/devices.py:1313` | `get_device_list_last_stream_id_for_remotes` (@cachedList) | `device_lists_remote_extremeties` | list on `user_ids` | `safe-explicit` | Keyed per full mxid. |
| `synapse/storage/databases/main/devices.py:1702` | `_get_min_device_lists_changes_in_room` | `device_lists_changes_in_room` | none (zero args) | `swap` | Zero-arg cache over tenant-scoped table. |
| `synapse/storage/databases/main/signatures.py:35` | `get_event_reference_hash` | `event_json` (via events cache) | `event_id` | `swap` | Opaque event_id; conservative. verify-in-Task-5 |
| `synapse/storage/databases/main/signatures.py:41` | `get_event_reference_hashes` (@cachedList) | `event_json` | list on `event_ids` | `swap` | Same. |
| `synapse/storage/databases/main/events_worker.py:1781` | `_have_seen_events_dict` (@cachedList for `have_seen_event`) | `events` | `room_id`, list on `event_ids` | `safe-explicit` | Cache key is (room_id, event_id); room_id qualifies. |
| `synapse/storage/databases/main/events_worker.py:1820` | `have_seen_event` | `events` | `room_id, event_id` | `safe-explicit` | room_id qualified. |
| `synapse/storage/databases/main/events_worker.py:2168` | `get_event_ordering` | `events` | `event_id, room_id` | `safe-explicit` | room_id qualified. |
| `synapse/storage/databases/main/events_worker.py:2504` | `get_partial_state_events` (@cachedList for `is_partial_state_event`) | `partial_state_events` | list on `event_ids` | `swap` | Key is bare event_id. verify-in-Task-5 |
| `synapse/storage/databases/main/events_worker.py:2531` | `is_partial_state_event` | `partial_state_events` | `event_id` | `swap` | Bare event_id. verify-in-Task-5 |
| `synapse/storage/databases/main/events_worker.py:2770` | `get_metadata_for_event` | `events` | `room_id, event_id` | `safe-explicit` | room_id qualified. |
| `synapse/storage/databases/main/event_federation.py:1486` | `get_latest_event_ids_in_room` | `event_forward_extremities` | `room_id` | `safe-explicit` | room_id qualified. |
| `synapse/storage/databases/main/event_federation.py:1586` | `_get_forward_extremeties_for_room` | `stream_ordering_to_exterm` | `room_id, stream_ordering` | `safe-explicit` | room_id qualified. |
| `synapse/storage/databases/main/pusher.py:318` | `get_if_user_has_pusher` | `pushers` | `user_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/registration.py:380` | `get_user_by_id` | `users` | `user_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/registration.py:467` | `get_user_by_access_token` | `access_tokens`, `users` | `token` | `swap` | **The canonical Finding #1 case** — bare token, no tenant context. |
| `synapse/storage/databases/main/registration.py:480` | `get_expiration_ts_for_user` | `account_validity` | `user_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/registration.py:675` | `is_server_admin` | `users` | `user` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/registration.py:812` | `is_real_user` | `users` | `user_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/registration.py:826` | `is_support_user` | `users` | `user_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/registration.py:1026` | `get_user_by_external_id` | `user_external_ids` | `auth_provider, external_id` | `swap` | Neither key includes tenant; same (provider, ext_id) pair can legitimately map to different users in each tenant. verify-in-Task-5 |
| `synapse/storage/databases/main/registration.py:1323` | `get_user_deactivated_status` | `users` | `user_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/registration.py:1344` | `get_user_locked_status` | `users` | `user_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/registration.py:1365` | `get_user_suspended_status` | `users` | `user_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/registration.py:1972` | `mark_access_token_as_used` | `access_tokens` | `token_id` | `swap` | Bare integer PK from per-tenant sequence; collides across tenants. verify-in-Task-5 |
| `synapse/storage/databases/main/registration.py:2221` | `is_guest` | `users` | `user_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/registration.py:2233` | `is_user_approved` | `users` | `user_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/thread_subscriptions.py:418` | `get_subscription_for_thread` | `thread_subscriptions` | `user_id, room_id, thread_root_event_id` | `safe-explicit` | Both user_id and room_id qualified. |
| `synapse/storage/databases/main/thread_subscriptions.py:474` | `get_subscribers_to_thread` | `thread_subscriptions` | `room_id, thread_root_event_id` | `safe-explicit` | room_id qualified. |
| `synapse/storage/databases/main/end_to_end_keys.py:204` | `_get_e2e_device_keys_for_federation_query_inner` | via `get_e2e_device_keys_and_signatures` | `user_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/end_to_end_keys.py:484` | `_get_e2e_cross_signing_signatures_for_device` | `e2e_cross_signing_keys`, `e2e_cross_signing_signatures` | `user_id_and_device_id (tuple)` | `safe-explicit` | user_id in tuple is full mxid. |
| `synapse/storage/databases/main/end_to_end_keys.py:495` | `_get_e2e_cross_signing_signatures_for_devices` (@cachedList) | same | list on `device_query` | `safe-explicit` | Same. |
| `synapse/storage/databases/main/end_to_end_keys.py:662` | `count_e2e_one_time_keys` | `e2e_one_time_keys_json` | `user_id, device_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/end_to_end_keys.py:877` | `get_e2e_unused_fallback_key_types` | `e2e_fallback_keys_json` | `user_id, device_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/end_to_end_keys.py:919` | `_get_bare_e2e_cross_signing_keys` | `e2e_cross_signing_keys` | `user_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/end_to_end_keys.py:928` | `_get_bare_e2e_cross_signing_keys_bulk` (@cachedList) | `e2e_cross_signing_keys` | list on `user_ids` | `safe-explicit` | Keyed per full mxid. |
| `synapse/storage/databases/main/presence.py:246` | `_get_presence_for_user` | `presence_stream` (via @cachedList impl) | `user_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/presence.py:250` | `get_presence_for_users` (@cachedList) | `presence_stream` | list on `user_ids` | `safe-explicit` | Keyed per full mxid. |
| `synapse/storage/databases/main/presence.py:315` | `_get_full_presence_stream_token_for_user` | `users_to_send_full_presence_to` | `user_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/experimental_features.py:46` | `list_enabled_features` | `per_user_experimental_features` | `user_id` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/experimental_features.py:101` | `is_feature_enabled` | `per_user_experimental_features` | `user_id, feature` | `safe-explicit` | Full mxid. |
| `synapse/storage/databases/main/state.py:156` | `get_room_version_id` | `rooms` | `room_id` | `safe-explicit` | room_id qualified. |
| `synapse/storage/databases/main/state.py:303` | `get_room_type` | `room_stats_state`, `room_stats_current` | `room_id` | `safe-explicit` | room_id qualified. |
| `synapse/storage/databases/main/state.py:323` | `bulk_get_room_type` (@cachedList) | same | list on `room_ids` | `safe-explicit` | Keyed per room_id. |
| `synapse/storage/databases/main/state.py:398` | `get_room_encryption` | `room_stats_state`, `room_stats_current` | `room_id` | `safe-explicit` | room_id qualified. |
| `synapse/storage/databases/main/state.py:402` | `bulk_get_room_encryption` (@cachedList) | same | list on `room_ids` | `safe-explicit` | Keyed per room_id. |
| `synapse/storage/databases/main/state.py:507` | `get_partial_current_state_ids` | `current_state_events` | `room_id` | `safe-explicit` | room_id qualified. |
| `synapse/storage/databases/main/state.py:604` | `_get_state_group_for_event` | `event_to_state_groups` | `event_id` | `swap` | Opaque event_id; conservative. verify-in-Task-5 |
| `synapse/storage/databases/main/state.py:614` | `_get_state_group_for_events` (@cachedList) | `event_to_state_groups` | list on `event_ids` | `swap` | Same. |
| `synapse/storage/databases/main/relations.py:162` | `get_relations_for_event` | `event_relations`, `events` | `room_id, event_id, event, ...` | `safe-explicit` | room_id qualified (event is uncached). |
| `synapse/storage/databases/main/relations.py:457` | `get_references_for_event` | `event_relations`, `events` | `event_id` | `swap` | Opaque event_id; conservative. verify-in-Task-5 |
| `synapse/storage/databases/main/relations.py:461` | `get_references_for_events` (@cachedList) | same | list on `event_ids` | `swap` | Same. |
| `synapse/storage/databases/main/relations.py:511` | `get_applicable_edit` | `event_relations`, `events` | `event_id` | `swap` | Opaque event_id; conservative. verify-in-Task-5 |
| `synapse/storage/databases/main/relations.py:516` | `get_applicable_edits` (@cachedList) | same | list on `event_ids` | `swap` | Same. |
| `synapse/storage/databases/main/relations.py:598` | `get_thread_summary` | `event_relations`, `events` | `event_id` | `swap` | Opaque event_id; conservative. verify-in-Task-5 |
| `synapse/storage/databases/main/relations.py:603` | `get_thread_summaries` (@cachedList) | same | list on `event_ids` | `swap` | Same. |
| `synapse/storage/databases/main/relations.py:773` | `get_thread_participated` | `event_relations`, `events` | `event_id, user_id` | `safe-explicit` | user_id full mxid disambiguates. |
| `synapse/storage/databases/main/relations.py:777` | `get_threads_participated` (@cachedList) | same | list on `event_ids`, `user_id` | `safe-explicit` | user_id disambiguates. |
| `synapse/storage/databases/main/relations.py:926` | `get_threads` | `threads` | `room_id, limit, from_token` | `safe-explicit` | room_id qualified. |
| `synapse/storage/databases/main/relations.py:990` | `get_thread_id` | `event_relations` | `event_id` | `swap` | Opaque event_id; conservative. verify-in-Task-5 |
| `synapse/storage/databases/main/relations.py:1052` | `get_thread_id_for_receipts` | `event_relations` | `event_id` | `swap` | Opaque event_id; conservative. verify-in-Task-5 |
| `synapse/storage/databases/state/store.py:157` | `get_state_group_delta` | `state_group_edges`, `state_groups_state` | `state_group` | `swap` | Bare integer from per-tenant `state_group_id_seq`; collides across tenants. verify-in-Task-5 |
| `synapse/storage/controllers/state.py:639` | `get_server_acl_for_room` | (current state read) | `room_id` | `safe-explicit` | room_id qualified. |
| `synapse/storage/controllers/state.py:817` | `_get_joined_hosts` | `current_state_events` (indirect) | `room_id, state_group, state_entry` | `safe-explicit` | room_id qualified; state_group adds refinement. |

## Summary

- **Total decorators:** 139
- **`swap`:** 35
- **`safe-explicit`:** 104
- **`not-tenant-scoped`:** 0
- **Entries with `verify-in-Task-5` note:** 19

## Actionable list (Task 5 input)

The 35 `swap` entries below get handled one-by-one in Task 5. Each will
be verified again before the actual decorator rewrite (the plan's budget
guard allows a `safe-explicit` re-classification if Task 5 finds the cache
key is in fact tenant-scoped after closer reading).

### `swap` list (file:line — method — cache-key summary)

1. `synapse/storage/databases/main/receipts.py:574` — `get_linearized_receipts_for_all_rooms` — stream tokens only.
2. `synapse/storage/databases/main/stream.py:2483` — `get_id_for_instance` — `instance_name`.
3. `synapse/storage/databases/main/stream.py:2521` — `get_name_from_instance_id` — `instance_id (int)`.
4. `synapse/storage/databases/main/transactions.py:169` — `get_destination_retry_timings` — remote `destination`.
5. `synapse/storage/databases/main/transactions.py:212` — `get_destination_retry_timings_batch` (@cachedList) — remote `destination` list.
6. `synapse/storage/databases/main/monthly_active_users.py:79` — `get_monthly_active_count` — zero args.
7. `synapse/storage/databases/main/monthly_active_users.py:102` — `get_monthly_active_count_by_service` — zero args.
8. `synapse/storage/databases/main/room.py:1542` — `get_partial_rooms` — zero args.
9. `synapse/storage/databases/main/keys.py:131` — `_get_server_keys_json` — `(server_name, key_id)` tuple.
10. `synapse/storage/databases/main/keys.py:137` — `get_server_keys_json` (@cachedList) — same.
11. `synapse/storage/databases/main/keys.py:199` — `get_server_key_json_for_remote` — `(server_name, key_id)`.
12. `synapse/storage/databases/main/keys.py:207` — `get_server_keys_json_for_remote` (@cachedList) — same.
13. `synapse/storage/databases/main/roommember.py:1060` — `_get_user_id_from_membership_event_id` — bare `event_id`.
14. `synapse/storage/databases/main/roommember.py:1071` — `_get_user_ids_from_membership_event_ids` (@cachedList) — bare `event_ids`.
15. `synapse/storage/databases/main/roommember.py:1407` — `_get_membership_from_event_id` — bare `member_event_id`.
16. `synapse/storage/databases/main/roommember.py:1413` — `get_membership_from_event_ids` (@cachedList) — bare `member_event_ids`.
17. `synapse/storage/databases/main/devices.py:1702` — `_get_min_device_lists_changes_in_room` — zero args.
18. `synapse/storage/databases/main/signatures.py:35` — `get_event_reference_hash` — bare `event_id`.
19. `synapse/storage/databases/main/signatures.py:41` — `get_event_reference_hashes` (@cachedList) — bare `event_ids`.
20. `synapse/storage/databases/main/events_worker.py:2504` — `get_partial_state_events` (@cachedList) — bare `event_ids`.
21. `synapse/storage/databases/main/events_worker.py:2531` — `is_partial_state_event` — bare `event_id`.
22. `synapse/storage/databases/main/registration.py:467` — `get_user_by_access_token` — **bare `token`** (Finding #1 canonical).
23. `synapse/storage/databases/main/registration.py:1026` — `get_user_by_external_id` — `(auth_provider, external_id)`.
24. `synapse/storage/databases/main/registration.py:1972` — `mark_access_token_as_used` — bare `token_id (int)`.
25. `synapse/storage/databases/main/state.py:604` — `_get_state_group_for_event` — bare `event_id`.
26. `synapse/storage/databases/main/state.py:614` — `_get_state_group_for_events` (@cachedList) — bare `event_ids`.
27. `synapse/storage/databases/main/relations.py:457` — `get_references_for_event` — bare `event_id`.
28. `synapse/storage/databases/main/relations.py:461` — `get_references_for_events` (@cachedList) — bare `event_ids`.
29. `synapse/storage/databases/main/relations.py:511` — `get_applicable_edit` — bare `event_id`.
30. `synapse/storage/databases/main/relations.py:516` — `get_applicable_edits` (@cachedList) — bare `event_ids`.
31. `synapse/storage/databases/main/relations.py:598` — `get_thread_summary` — bare `event_id`.
32. `synapse/storage/databases/main/relations.py:603` — `get_thread_summaries` (@cachedList) — bare `event_ids`.
33. `synapse/storage/databases/main/relations.py:990` — `get_thread_id` — bare `event_id`.
34. `synapse/storage/databases/main/relations.py:1052` — `get_thread_id_for_receipts` — bare `event_id`.
35. `synapse/storage/databases/state/store.py:157` — `get_state_group_delta` — bare `state_group (int)`.

### Already-safe list (`safe-explicit`, 104 entries)

These are left unchanged — their cache keys already include Matrix-qualified
user_id or room_id that disambiguate per tenant. See the classification
table above for the per-entry reasoning.

## Known gaps

1. **Matrix room_ids are globally unique strings, but per-tenant views can
   diverge for remote rooms.** A room_id like `!abc:remote.example` is the
   same string in both tenants if both are joined to that remote room, yet
   the per-tenant `current_state_events` (and related tables) may hold
   different state. Under this audit's heuristic (per plan), a `room_id`
   key is marked `safe-explicit`. In practice, the cache content will be
   whichever tenant populated it first — risking stale / wrong data for
   the other tenant's view of the same remote room. If Finding #1's
   mitigation path `@tenant_cached` is cheap enough, Task 5 may want to
   revisit these in a follow-up pass.

2. **`_get_joined_hosts` (`controllers/state.py:817`) and
   `_get_joined_hosts_cache` (`roommember.py:1274`)** cache a mutable
   `_JoinedHostsCache` object keyed on `room_id`. Same remote-room caveat
   as above.

3. **`@cached` methods that take a `cache_context` parameter** propagate
   cache invalidation to downstream cached methods. A `@tenant_cached`
   drop-in must preserve `cache_context` propagation otherwise the
   downstream caches will not be invalidated when the tenant-scoped cache
   entry is evicted. Relevant entries: `appservice.py:199`,
   `roommember.py:985`.

## Further reading: `@cached` / `@cachedList` outside `synapse/storage/`

Out of scope for this audit per Task 3, but worth noting for future work:
other `@cached` / `@cachedList` decorators exist in handler-layer files
(e.g. `synapse/federation/`, `synapse/handlers/`). A follow-up audit
should enumerate those to confirm they don't re-introduce the cross-tenant
leak patterns found above. `ApplicationService` and federation senders
are particular candidates.
