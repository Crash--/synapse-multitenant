// Single source of narrative truth.
// Each stage has start/end ms (relative to playback start), label,
// fileRef (real repo path), code (Python snippet), logLines, plus
// upstream-equivalent fields used by Scene D for the side-by-side
// comparison with vanilla (single-tenant) Synapse.
//
// Stages 1-13: request lifecycle (phases 1-2)
// Stages 14-19: per-tenant config & isolation (phases 3-5)
// Stages 20-22: hot reload & backup (phase 6)
// Stages 23-26: dynamic control plane (phase 7)

export const TENANTS = {
  acme: { id: "acme", host: "acme.localhost", schema: "tenant_acme", color: "#22d3ee" },
  corp: { id: "corp", host: "corp.localhost", schema: "tenant_corp", color: "#f59e0b" },
  startup: { id: "startup", host: "startup.localhost", schema: "tenant_startup", color: "#a78bfa" },
};

export const PARALLEL_OFFSET_MS = 200;

export const STAGES = [
  // ── Request lifecycle (phases 1-2) ───────────────────────────────
  {
    id: "client",
    label: "Client request",
    component: "client",
    layer: "network",
    startMs: 0,
    endMs: 1500,
    fileRef: null,
    code: `GET /_matrix/client/versions HTTP/1.1
Host: acme.localhost
Accept: application/json`,
    codeLang: "http",
    logLines: ["req GET /_matrix/client/versions"],
    upstream: "uclient",
    upstreamFileRef: null,
    upstreamCode: `GET /_matrix/client/versions HTTP/1.1
Host: matrix.example.org
Accept: application/json`,
    upstreamCodeLang: "http",
  },
  {
    id: "proxy",
    label: "Reverse proxy (Traefik / nginx)",
    component: "traefik",
    layer: "network",
    startMs: 1500,
    endMs: 3000,
    fileRef: "docker-demo/docker-compose.yml",
    code: `# Traefik routes by Host header to Synapse
# (nginx works too — docker-multitenant uses nginx)
traefik.http.routers.synapse.rule=
  Host(\`matrix.tenant-a.com\`) ||
  Host(\`matrix.tenant-b.com\`)
traefik.http.services.synapse.
  loadbalancer.server.port=8008`,
    codeLang: "nginx",
    logLines: ["proxy forwarded Host header → synapse:8008"],
    upstream: "unginx",
    upstreamFileRef: null,
    upstreamCode: `location /_matrix {
    proxy_pass http://synapse:8008;
    # Single homeserver — Host header
    # is informational only.
}`,
    upstreamCodeLang: "nginx",
  },
  {
    id: "router",
    label: "Synapse · TenantRouter",
    component: "router",
    layer: "routing",
    startMs: 3000,
    endMs: 5000,
    fileRef: "synapse/tenant_registry.py",
    code: `def get_tenant_by_host(host: str) -> TenantConfig | None:
    """Look up the tenant for an incoming Host header."""
    tenant = self._by_server_name.get(host)
    if tenant is None:
        tenant = self._by_alias.get(host)
    return tenant`,
    codeLang: "python",
    logLines: ["tenant resolved from Host header"],
    upstream: "usynapse",
    upstreamFileRef: "synapse/server.py",
    upstreamCode: `# Upstream Synapse has no per-request routing.
# A single HomeServer instance is created at
# startup with one server_name baked in.
class HomeServer:
    hostname: str  # set once from config`,
    upstreamCodeLang: "python",
  },
  {
    id: "config",
    label: "TenantConfig resolved",
    component: "router",
    layer: "routing",
    startMs: 5000,
    endMs: 6500,
    fileRef: "synapse/config/tenants.py",
    code: `@attr.s(auto_attribs=True, slots=True, frozen=True)
class TenantConfig:
    server_name: str          # "acme.localhost"
    database_schema: str      # "tenant_acme"
    signing_key_path: str     # from YAML or None (DB)
    signing_key_data: str     # from DB (AES-256-GCM decrypted)
    media_store_path: str     # ".../media/acme"
    registration_enabled: bool
    enable_federation: bool
    # Phase 3-4: per-tenant sub-configs
    oidc_config: TenantOidcConfig | None
    email_config: TenantEmailConfig | None
    push_config: TenantPushConfig | None
    ratelimit: TenantRatelimitConfig | None`,
    codeLang: "python",
    logLines: ["TenantConfig{server_name=acme.localhost, schema=tenant_acme}"],
    upstream: "usynapse",
    upstreamFileRef: "synapse/config/server.py",
    upstreamCode: `class ServerConfig(Config):
    server_name: str          # loaded once at startup
    signing_key_path: str     # one global key
    # database & media settings live in
    # DatabaseConfig / ContentRepositoryConfig`,
    upstreamCodeLang: "python",
  },
  {
    id: "context",
    label: "Bind tenant to ContextVar",
    component: "context",
    layer: "context",
    startMs: 6500,
    endMs: 8000,
    fileRef: "synapse/tenant_context.py",
    code: `_current_tenant: ContextVar[TenantConfig | None] = ContextVar(
    "current_tenant", default=None,
)

def set_current_tenant(tenant: TenantConfig) -> None:
    _current_tenant.set(tenant)

# Every downstream call uses get_current_tenant()
# to discover DB schema, signing key, media root,
# SSO config, push config, rate limits, etc.`,
    codeLang: "python",
    logLines: ["context bound: tenant_acme"],
    upstream: "usynapse",
    upstreamFileRef: null,
    upstreamCode: `# Upstream Synapse has no per-request tenant
# context. Code reads self.hs.hostname directly
# wherever it needs the server name — there is
# only ever one tenant per process.`,
    upstreamCodeLang: "python",
  },
  {
    id: "schema",
    label: "Postgres search_path switch",
    component: "postgres",
    layer: "storage",
    startMs: 8000,
    endMs: 10000,
    fileRef: "synapse/storage/database.py",
    code: `tenant = get_current_tenant()
if tenant is not None:
    # Phase 8: session-level SET + connection cache.
    # Cache check — skip SET if conn already has this schema.
    if self._connection_schemas.get(id(conn)) != schema:
        cursor.execute(f'SET search_path TO {schema}')
        self._connection_schemas[id(conn)] = schema
# Each tenant's tables live in their own PG schema.
# Sequences, indexes, and constraints are all cloned.
# No SHOW or restore needed — session SET persists.`,
    codeLang: "python",
    logLines: [
      "db: SET search_path TO tenant_acme (cached — skip if unchanged)",
      "db: pgbouncer session mode, cp_max=50, max_connections=200",
    ],
    upstream: "upostgres",
    upstreamFileRef: "synapse/storage/database.py",
    upstreamCode: `# One database, no schema switching.
# Every query targets the public schema.
txn.execute(sql, args)`,
    upstreamCodeLang: "python",
  },
  {
    id: "keyring",
    label: "MultiTenantKeyring lookup",
    component: "keyring",
    layer: "storage",
    startMs: 10000,
    endMs: 11500,
    fileRef: "synapse/crypto/multitenant_keyring.py",
    code: `def get_signing_key(self, server_name: str) -> SigningKey:
    """Return the signing key for a tenant's server_name.
    Keys can come from:
      - filesystem (YAML source)
      - in-memory (DB source, AES-256-GCM decrypted)"""
    return self._keys_by_server[server_name]`,
    codeLang: "python",
    logLines: ["keyring: loaded ed25519:auto acme.signing.key"],
    upstream: "ukeyring",
    upstreamFileRef: "synapse/crypto/keyring.py",
    upstreamCode: `class Keyring:
    """One keyring, one signing key — the homeserver's."""
    def __init__(self, hs):
        self.hs = hs
        self._signing_key = hs.signing_key`,
    upstreamCodeLang: "python",
  },
  {
    id: "media",
    label: "Media storage tenant-aware",
    component: "media",
    layer: "storage",
    startMs: 11500,
    endMs: 13000,
    fileRef: "synapse/media/multitenant_filepath.py",
    code: `# FileStorageProviderBackend resolves tenant path
def _tenant_base(self, base: str) -> str:
    tenant = get_current_tenant()
    if tenant is not None:
        return os.path.join(base, tenant.server_name)
    return base

# MediaStorage routes local I/O through _local_path
def _local_path(self, rel_path: str) -> str:
    base = self.local_media_directory
    tenant = get_current_tenant()
    if tenant is not None:
        base = os.path.join(base, tenant.server_name)
    return os.path.join(base, rel_path)`,
    codeLang: "python",
    logLines: [
      "media root: /var/synapse/media/acme.com",
      "FileStorageProviderBackend._tenant_base → <base>/acme.com/",
      "MediaStorage._local_path → tenant-scoped local I/O",
      "URL previewer + thumbnailer: flows through tenant-aware MediaStorage",
    ],
    upstream: "umedia",
    upstreamFileRef: "synapse/media/filepath.py",
    upstreamCode: `class MediaFilePaths:
    def __init__(self, primary_base_path: str):
        self.base_path = primary_base_path
    # Single root, no tenant lookup.`,
    upstreamCodeLang: "python",
  },
  {
    id: "response_built",
    label: "Response built (context still bound)",
    component: "context",
    layer: "response",
    startMs: 13000,
    endMs: 14400,
    fileRef: "synapse/tenant_context.py",
    code: `# Handler finishes; the response object is built while
# the ContextVar is still bound to tenant_acme — so
# late-stage hooks see the right tenant:
#   • rate limiting (TenantRatelimiterRegistry)
#   • push config (TenantPushConfig)
#   • app services (TenantAppServiceRegistry)
tenant = get_current_tenant()
assert tenant.server_name == "acme.localhost"`,
    codeLang: "python",
    logLines: ["response built · tenant still bound · rate limits per-tenant"],
    upstream: "usynapse",
    upstreamFileRef: null,
    upstreamCode: `# Handler returns. There is nothing to "unbind" —
# upstream Synapse never bound a tenant in the
# first place.`,
    upstreamCodeLang: "python",
  },
  {
    id: "response_proxy",
    label: "Response · Synapse → Proxy",
    component: "traefik",
    layer: "response",
    startMs: 14400,
    endMs: 15800,
    fileRef: null,
    code: `# Reverse proxy receives the upstream response and
# forwards it back to the client unchanged.
# No tenant awareness here — that lived inside
# the Synapse process.`,
    codeLang: "nginx",
    logLines: ["proxy ← synapse 200 OK"],
    upstream: "unginx",
    upstreamFileRef: null,
    upstreamCode: `# Same as multi-tenant — proxy is just a
# transparent reverse proxy.
proxy_pass http://synapse:8008;`,
    upstreamCodeLang: "nginx",
  },
  {
    id: "response_client",
    label: "Response · Proxy → Client",
    component: "client",
    layer: "response",
    startMs: 15800,
    endMs: 17200,
    fileRef: null,
    code: `HTTP/1.1 200 OK
Content-Type: application/json

{ "versions": ["v1.13", "v1.14"] }`,
    codeLang: "http",
    logLines: ["client ← 200 OK"],
    upstream: "uclient",
    upstreamFileRef: null,
    upstreamCode: `HTTP/1.1 200 OK
Content-Type: application/json

{ "versions": ["v1.13", "v1.14"] }`,
    upstreamCodeLang: "http",
  },
  {
    id: "bgproc",
    label: "Background processes, per tenant",
    component: "context",
    layer: "context",
    startMs: 17200,
    endMs: 19400,
    fileRef: "synapse/tenant_background.py",
    code: `def run_as_background_process_per_tenant(
    desc, hs, func, *args, **kwargs,
):
    """One independent bg process per tenant, each with
    the ContextVar bound so the DB schema, keyring, and
    media root resolve to the right tenant."""
    registry = hs.get_tenant_registry()
    for tenant in registry.get_all_tenants():
        async def _runner(_t=tenant):
            with tenant_context(_t):
                return await func(*args, **kwargs)
        deferreds.append(
            run_as_background_process(
                desc, tenant.server_name, _runner,
            )
        )`,
    codeLang: "python",
    logLines: [
      "bgproc user_directory.notify_new_event · ContextVar bound",
      "bgproc stats.notify_new_event · ContextVar bound",
      "bgproc purge_history_for_rooms_in_range · ContextVar bound",
      "bgproc send_renewals · ContextVar bound",
      "bgproc user_parter_loop · ContextVar bound",
      "bgproc db: search_path → tenant schema",
    ],
    upstream: "usynapse",
    upstreamFileRef: "synapse/metrics/background_process_metrics.py",
    upstreamCode: `# Upstream Synapse schedules each bg loop once
# per process, labelled with the single global
# hs.hostname. No tenant binding — there is only
# ever one tenant in the process.
run_as_background_process(
    "user_directory.notify_new_event",
    hs.hostname,
    process,
)`,
    upstreamCodeLang: "python",
  },
  {
    id: "recap_request",
    label: "Isolation recap",
    component: "all",
    layer: "response",
    startMs: 19400,
    endMs: 21400,
    fileRef: null,
    code: `# Three tenants, one process, zero shared state:
#   acme.localhost    ->  tenant_acme
#   corp.localhost    ->  tenant_corp
#   startup.localhost ->  tenant_startup
#
# Each tenant has its own:
#   • Postgres schema (search_path per txn)
#   • Signing key (MultiTenantKeyring)
#   • Media root (tenant-scoped paths)
#   • Background processes (ContextVar bound)`,
    codeLang: "python",
    logLines: ["all tenants isolated · 1 process"],
    upstream: "all",
    upstreamFileRef: null,
    upstreamCode: `# Upstream Synapse:
#   1 process == 1 server_name
#
# To host N homeservers you need N processes,
# N databases (or N PG users), N media roots,
# N signing keys, N nginx upstreams.`,
    upstreamCodeLang: "python",
  },

  // ── Per-tenant config (phases 3-4) ──────────────────────────────
  {
    id: "sso",
    label: "Per-tenant SSO (OIDC / CAS / SAML)",
    component: "handlers",
    layer: "tenant_config",
    startMs: 21400,
    endMs: 23400,
    fileRef: "synapse/handlers/oidc.py",
    code: `# Each tenant can have its own OIDC / CAS / SAML provider.
# Config lives in TenantConfig sub-objects:

class TenantOidcConfig:
    providers: list[dict]     # raw OIDC provider dicts

class TenantCasConfig:
    server_url: str
    displayname_attribute: str

class TenantSamlConfig:
    idp_name: str
    idp_entityid: str

# OidcHandler resolves providers per-tenant at request time:
def _get_oidc_providers_for_tenant(self):
    tenant = get_current_tenant()
    return tenant.oidc_config.providers`,
    codeLang: "python",
    logLines: [
      "SSO: OIDC provider dispatch per-tenant",
      "SSO: CAS handler resolves config from tenant context",
      "SSO: SAML handler resolves config from tenant context",
    ],
    upstream: "usynapse",
    upstreamFileRef: "synapse/handlers/oidc.py",
    upstreamCode: `# Upstream: one OIDC/CAS/SAML config for the
# entire server. Providers loaded once at startup
# from homeserver.yaml.
class OidcHandler:
    def __init__(self, hs):
        self._providers = parse_providers(
            hs.config.oidc.oidc_providers
        )`,
    upstreamCodeLang: "python",
  },
  {
    id: "email_push",
    label: "Per-tenant email & push",
    component: "handlers",
    layer: "tenant_config",
    startMs: 23400,
    endMs: 25400,
    fileRef: "synapse/handlers/send_email.py",
    code: `# Each tenant has its own SMTP settings and push config.

class TenantEmailConfig:
    smtp_host: str
    smtp_port: int
    notif_from: str       # "Your Server <noreply@acme.com>"
    enable_tls: bool

class TenantPushConfig:
    include_content: bool
    group_unread_count_by_room: bool

# SendEmailHandler resolves SMTP from tenant context:
async def send_email(self, ...):
    tenant = get_current_tenant()
    smtp = tenant.email_config or self._default_config`,
    codeLang: "python",
    logLines: [
      "email: SMTP config resolved from tenant context",
      "push: HttpPusher uses per-tenant push config",
      "push: EmailPusher routes through tenant-aware SendEmailHandler",
    ],
    upstream: "usynapse",
    upstreamFileRef: "synapse/handlers/send_email.py",
    upstreamCode: `# Upstream: one SMTP config, one push config.
# All users get the same email sender and push
# behavior regardless of domain.
class SendEmailHandler:
    def __init__(self, hs):
        self.smtp_host = hs.config.email.smtp_host`,
    upstreamCodeLang: "python",
  },
  {
    id: "ratelimit",
    label: "Per-tenant rate limiting",
    component: "ratelimiter",
    layer: "tenant_config",
    startMs: 25400,
    endMs: 27400,
    fileRef: "synapse/api/tenant_ratelimiting.py",
    code: `class TenantRatelimitConfig:
    """17 RatelimitSettings fields, each None = inherit global."""
    rc_message: RatelimitSettings | None
    rc_login: RatelimitSettings | None
    rc_registration: RatelimitSettings | None
    rc_joins_local: RatelimitSettings | None
    # ... 13 more ...

class TenantRatelimiterRegistry:
    """Lazily creates per-tenant Ratelimiter instances.
    Cached by (server_name, limiter_key)."""

    def get_ratelimiter(self, key: str) -> Ratelimiter:
        tenant = get_current_tenant()
        cache_key = (tenant.server_name, key)
        return self._cache[cache_key]  # or create new`,
    codeLang: "python",
    logLines: [
      "ratelimit: TenantRatelimiterRegistry resolved for acme",
      "ratelimit: rc_message → per_second=10, burst_count=50",
      "ratelimit: 12 handler/REST sites converted",
    ],
    upstream: "usynapse",
    upstreamFileRef: "synapse/api/ratelimiting.py",
    upstreamCode: `# Upstream: global rate limits from config.
# All users share the same buckets and thresholds.
class Ratelimiter:
    def __init__(self, ...):
        self.rate = hs.config.ratelimiting.rc_message`,
    upstreamCodeLang: "python",
  },
  {
    id: "appservice",
    label: "Per-tenant app services",
    component: "appservices",
    layer: "tenant_config",
    startMs: 27400,
    endMs: 29400,
    fileRef: "synapse/appservice/tenant_registry.py",
    code: `class TenantAppServiceRegistry:
    """Per-tenant application service lists.
    Each tenant has its own bridges/bots."""

    def get_app_services(self) -> list[AppService]:
        tenant = get_current_tenant()
        return self._by_tenant[tenant.server_name]

    def reload(self, registry: TenantRegistry):
        """Hot reload: add/remove AS lists for tenants."""
        for tenant in registry.get_all_tenants():
            self._by_tenant[tenant.server_name] = (
                load_as_configs(tenant.app_service_config_files)
            )`,
    codeLang: "python",
    logLines: [
      "appservice: loaded 2 bridges for acme.localhost",
      "appservice: loaded 0 bridges for corp.localhost",
      "appservice: get_app_services() dispatches on tenant context",
    ],
    upstream: "usynapse",
    upstreamFileRef: "synapse/appservice/api.py",
    upstreamCode: `# Upstream: one global AS list. All tenants
# would share the same bridges and bots.
class ApplicationServiceWorkerStore:
    services_cache: list[AppService]  # loaded once`,
    upstreamCodeLang: "python",
  },

  // ── Hot reload & backup (phase 6) ───────────────────────────────
  {
    id: "hot_reload",
    label: "Hot reload (SIGHUP / HTTP)",
    component: "router",
    layer: "lifecycle",
    startMs: 29400,
    endMs: 31800,
    fileRef: "synapse/rest/admin/tenants.py",
    code: `# Two reload triggers, same cascade:
# 1. SIGHUP signal (operator sends: kill -HUP <pid>)
# 2. HTTP POST /_synapse/admin/v1/tenants/reload
#    (control plane pushes after provisioning)

class ReloadTenantsRestServlet(RestServlet):
    async def on_POST(self, request):
        self._check_bearer_token(request)  # reload_secret

        if source == "database":
            new_cfg = load_tenants_from_database(conn)
        else:
            new_cfg = re_read_yaml_config()

        # Reload cascade — all registries update in-place:
        registry.reload(new_cfg)        # tenants
        keyring.reload(registry)        # signing keys
        as_registry.reload(registry)    # app services
        rl_registry.reload(registry)    # rate limiters`,
    codeLang: "python",
    logLines: [
      "reload: received trigger (SIGHUP or HTTP POST)",
      "reload: TenantRegistry.reload() — 1 added, 0 removed",
      "reload: MultiTenantKeyring.reload() — keys updated",
      "reload: TenantAppServiceRegistry.reload()",
      "reload: TenantRatelimiterRegistry.reload()",
    ],
    upstream: "usynapse",
    upstreamFileRef: null,
    upstreamCode: `# Upstream Synapse: no hot tenant reload.
# Changing server_name or adding a new homeserver
# requires a full process restart.
# There is no concept of tenant add/remove.`,
    upstreamCodeLang: "python",
  },
  {
    id: "backup_restore",
    label: "Backup / restore / drop",
    component: "postgres",
    layer: "lifecycle",
    startMs: 31800,
    endMs: 34000,
    fileRef: "scripts/synapse_tenant",
    code: `# synapse_tenant CLI — operator tooling

# backup: pg_dump tenant schema + tar media → .tar.gz
$ synapse_tenant backup \\
    --server-name acme.com --output acme.tar.gz
# Archive: manifest.json + schema.sql + media/

# restore: extract, validate manifest, psql + media
$ synapse_tenant restore \\
    --server-name acme.com --from acme.tar.gz

# drop: destructive removal (schema + media)
$ synapse_tenant drop \\
    --server-name acme.com --confirm-destructive
# DROP SCHEMA tenant_acme CASCADE; rm -rf media/acme`,
    codeLang: "python",
    logLines: [
      "backup: pg_dump tenant_acme → schema.sql",
      "backup: tar media/acme.com → archive",
      "backup: manifest.json written",
      "restore: validate manifest → restore schema → restore media",
      "drop: requires --confirm-destructive flag",
    ],
    upstream: "usynapse",
    upstreamFileRef: null,
    upstreamCode: `# Upstream Synapse: full-database backup only.
# No per-tenant granularity — backup everything
# or nothing. No tenant drop command.
$ pg_dump synapse_db > full_backup.sql`,
    upstreamCodeLang: "python",
  },

  // ── Dynamic control plane (phase 7) ─────────────────────────────
  {
    id: "control_plane",
    label: "Control plane (external service)",
    component: "controlplane",
    layer: "lifecycle",
    startMs: 34000,
    endMs: 36500,
    fileRef: "docker-demo/control-plane/src/routes/tenants.ts",
    code: `// Standalone TypeScript/Fastify service
// Manages the full tenant lifecycle via REST API.
// Synapse reads tenants from public.tenants table.

POST   /api/v1/tenants              // create
GET    /api/v1/tenants              // list
GET    /api/v1/tenants/:name        // get
PATCH  /api/v1/tenants/:name        // update
DELETE /api/v1/tenants/:name        // soft-delete
POST   /api/v1/tenants/:name/suspend
POST   /api/v1/tenants/:name/activate

// After each mutation → push reload to Synapse:
await reloadSynapseTenants(synapseUrl, secret)`,
    codeLang: "python",
    logLines: [
      "control-plane: POST /api/v1/tenants",
      "control-plane: bearer token validated",
      "control-plane: CRUD + lifecycle endpoints ready",
    ],
    upstream: "usynapse",
    upstreamFileRef: null,
    upstreamCode: `# Upstream Synapse: no tenant management API.
# No control plane. Each homeserver is a separate
# process configured by its own homeserver.yaml.
# Scaling = more processes, more configs, more ops.`,
    upstreamCodeLang: "python",
  },
  {
    id: "provisioning",
    label: "Provisioning pipeline",
    component: "controlplane",
    layer: "lifecycle",
    startMs: 36500,
    endMs: 39000,
    fileRef: "docker-demo/control-plane/src/services/provisioning.ts",
    code: `// Full tenant creation in one API call:

async function provisionTenant(serverName) {
  // 1. Validate server name (hostname format)
  // 2. Generate Ed25519 signing key (tweetnacl)
  // 3. Encrypt key with AES-256-GCM master key
  // 4. INSERT into public.tenants (status: provisioning)
  // 5. CREATE SCHEMA + clone tables from public
  //    (skips 'tenants' table itself)
  // 6. Clone sequences + copy singleton seed rows
  // 7. Create media directory on disk
  // 8. UPDATE status → 'active'
  // 9. POST /_synapse/admin/v1/tenants/reload
  //    → Synapse picks up the new tenant immediately
}`,
    codeLang: "python",
    logLines: [
      "provision: generate Ed25519 signing key",
      "provision: encrypt key (AES-256-GCM)",
      "provision: INSERT tenant row → provisioning",
      "provision: CREATE SCHEMA + clone 168 tables",
      "provision: clone sequences + seed rows",
      "provision: mkdir media directory",
      "provision: UPDATE status → active",
      "provision: POST synapse reload → tenant live",
    ],
    upstream: "usynapse",
    upstreamFileRef: null,
    upstreamCode: `# Upstream: no automated provisioning.
# Manual steps to add a new homeserver:
# 1. Write a new homeserver.yaml
# 2. Generate signing key
# 3. Create database
# 4. Run migrations
# 5. Configure nginx upstream
# 6. Start new Synapse process
# 7. Monitor + operate separately`,
    upstreamCodeLang: "python",
  },
  {
    id: "key_encryption",
    label: "Signing key encryption",
    component: "keyring",
    layer: "lifecycle",
    startMs: 39000,
    endMs: 41000,
    fileRef: "synapse/crypto/tenant_key_encryption.py",
    code: `# AES-256-GCM encryption — cross-language compatible
# between Python (cryptography lib) and TypeScript (node:crypto)

# Wire format: IV(12B) || ciphertext || tag(16B)

def encrypt_signing_key(key_text, master_key):
    iv = os.urandom(12)
    aesgcm = AESGCM(master_key)
    ct_with_tag = aesgcm.encrypt(iv, key_text.encode(), None)
    return iv + ct_with_tag

def decrypt_signing_key(encrypted, master_key):
    iv, ct = encrypted[:12], encrypted[12:]
    aesgcm = AESGCM(master_key)
    return aesgcm.decrypt(iv, ct, None).decode()

# Master key: SYNAPSE_TENANT_KEY_MASTER env var (base64, 32B)`,
    codeLang: "python",
    logLines: [
      "key: AES-256-GCM encrypt/decrypt",
      "key: wire format = IV(12B) || ciphertext || tag(16B)",
      "key: cross-language compat: Python ↔ TypeScript",
    ],
    upstream: "usynapse",
    upstreamFileRef: null,
    upstreamCode: `# Upstream: signing key stored as plain text file
# on disk. No encryption, no DB storage.
# Path: /etc/synapse/<server>.signing.key`,
    upstreamCodeLang: "python",
  },
  {
    id: "tenant_db",
    label: "Tenants DB table (source of truth)",
    component: "postgres",
    layer: "lifecycle",
    startMs: 41000,
    endMs: 43000,
    fileRef: "docker-demo/scripts/create_tenants_table.sql",
    code: `-- public.tenants: the database-driven tenant registry
CREATE TABLE public.tenants (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  server_name       TEXT NOT NULL UNIQUE,
  database_schema   TEXT NOT NULL UNIQUE,
  status            TEXT NOT NULL DEFAULT 'provisioning'
    CHECK (status IN ('provisioning','active','suspended','deleting')),

  signing_key_encrypted  BYTEA NOT NULL,  -- AES-256-GCM
  signing_key_id         TEXT  NOT NULL,

  -- Per-tenant sub-configs (JSONB)
  email_config     JSONB,   oidc_config      JSONB,
  push_config      JSONB,   ratelimit_config JSONB,
  cas_config       JSONB,   saml_config      JSONB,

  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Synapse reads: WHERE status = 'active'`,
    codeLang: "python",
    logLines: [
      "db: public.tenants table — single source of truth",
      "db: status lifecycle: provisioning → active → suspended → deleting",
      "db: JSONB columns for complex sub-configs",
    ],
    upstream: "upostgres",
    upstreamFileRef: null,
    upstreamCode: `# Upstream: no tenants table. No database-driven
# tenant config. Everything comes from YAML.
# homeserver.yaml is the only source of truth.`,
    upstreamCodeLang: "python",
  },

  // ── Final recap ─────────────────────────────────────────────────
  {
    id: "recap_full",
    label: "Full architecture recap",
    component: "all",
    layer: "lifecycle",
    startMs: 43000,
    endMs: 46000,
    fileRef: null,
    code: `# Multi-Tenant Synapse — Full Architecture
#
# EXTERNAL               SYNAPSE PROCESS           STORAGE
# ┌──────────┐  ┌─────────────────────────────┐  ┌──────────┐
# │ Control  │→ │ TenantRouter → ContextVar   │  │ Postgres │
# │  Plane   │  │   ↕ reload                  │  │ (schema/ │
# │ (Fastify)│  │ MultiKeyring  Media root    │  │  tenant) │
# └──────────┘  │ RateLimiter   AppServices   │  └──────────┘
# ┌──────────┐  │ SSO/Email/Push per-tenant   │  ┌──────────┐
# │ Traefik  │→ │ Handlers ↔ DataStore        │  │  Media   │
# │ (proxy)  │  └─────────────────────────────┘  │ (files)  │
# └──────────┘                                   └──────────┘
#
# Phases 1-10 landed. Phase 10' fix-up in progress.
#
# Phase 9 resolved:
#   ✅ Outbound federation: (tenant, dest) keyed queues
#   ✅ Signing: per-tenant key via MultiTenantKeyring
#   ✅ Event loop: per-event-batch tenant dispatch
#   ✅ EDU origin: tenant server_name on all EDUs
# Phase 10 resolved:
#   ✅ Inbound federation: X-Matrix destination → tenant context
#   ✅ FederationServer: _effective_server_name for EDUs/signing
#   ✅ Per-tenant federation allow-lists (TenantFederationConfig)
#   ✅ EventAuthHandler: tenant-aware is_host_joined
# Phase 10' bugs (surfaced by e2e tests):
#   🔴 Key server returns global keys (no tenant context on unauth endpoints)
#   🔴 Cross-tenant room join fails (sibling tenants seen as remote)
# Remaining bottlenecks:
#   • Phase 10' fix-up (key server + cross-tenant join)
#   • E2EE audit pass (phase 11)
#   • Schema cloning time for 168+ tables
#   • Single-process ceiling (no workers yet)
#   • GIL hard ceiling at ~100-200 tenants`,
    codeLang: "python",
    logLines: [
      "recap: 10 phases landed — federation inbound + outbound",
      "recap: 1 process, N tenants, 0 shared state",
      "recap: DB-driven tenancy, zero-tenant boot, HTTP push reload",
      "recap: pool tuned: pgbouncer + schema caching → ~100-200 ceiling",
      "recap: federation out: per-tenant queues, signing, EDU origin",
      "recap: federation in: X-Matrix dest routing, allow-lists",
      "recap: 10' fix-up needed: key server tenant ctx + cross-tenant join",
    ],
    upstream: "all",
    upstreamFileRef: null,
    upstreamCode: `# Upstream Synapse:
#   1 process == 1 server_name == ~150MB RAM
#
# To host 100 homeservers:
#   • 100 processes × 150MB = 15GB RAM baseline
#   • 100 homeserver.yaml files
#   • 100 signing keys on disk
#   • 100 nginx upstream entries
#   • 100 systemd units to operate
#   • No shared connection pool
#   • No centralized management API`,
    upstreamCodeLang: "python",
  },
];

export const TOTAL_DURATION_MS = STAGES[STAGES.length - 1].endMs;
