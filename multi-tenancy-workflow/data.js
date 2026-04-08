// Single source of narrative truth.
// Each stage has start/end ms (relative to playback start), label,
// fileRef (real repo path), code (Python snippet), logLines, plus
// upstream-equivalent fields used by Scene D for the side-by-side
// comparison with vanilla (single-tenant) Synapse.

export const TENANTS = {
  acme: { id: "acme", host: "acme.localhost", schema: "tenant_acme", color: "#22d3ee" },
  corp: { id: "corp", host: "corp.localhost", schema: "tenant_corp", color: "#f59e0b" },
  startup: { id: "startup", host: "startup.localhost", schema: "tenant_startup", color: "#a78bfa" },
};

export const PARALLEL_OFFSET_MS = 200;

export const STAGES = [
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
    label: "Reverse proxy (nginx)",
    component: "nginx",
    layer: "network",
    startMs: 1500,
    endMs: 3000,
    fileRef: "docker-multitenant/nginx/nginx.conf",
    code: `location /_matrix {
    proxy_pass http://synapse:8008;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
}`,
    codeLang: "nginx",
    logLines: ["nginx forwarded Host header"],
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
    code: `@dataclass
class TenantConfig:
    server_name: str          # "acme.localhost"
    database_schema: str      # "tenant_acme"
    signing_key_path: str     # ".../keys/acme.signing.key"
    media_store_path: str     # ".../media/acme"
    registration_enabled: bool
    federation_enabled: bool`,
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
    _current_tenant.set(tenant)`,
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
    txn.execute(
        f'SET search_path TO {tenant.database_schema}, public'
    )`,
    codeLang: "python",
    logLines: ["db: SET search_path TO tenant_acme, public"],
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
    """Return the signing key for a tenant's server_name."""
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
    label: "Media path resolved",
    component: "media",
    layer: "storage",
    startMs: 11500,
    endMs: 13000,
    fileRef: "synapse/media/multitenant_filepath.py",
    code: `def resolve(self, *parts: str) -> str:
    tenant = get_current_tenant()
    root = tenant.media_store_path if tenant else self._default_root
    return os.path.join(root, *parts)`,
    codeLang: "python",
    logLines: ["media root: /var/synapse/media/acme"],
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
# the ContextVar is still bound to tenant_acme — so any
# late-stage hooks (rate limiting, push, etc.) still see
# the right tenant.
tenant = get_current_tenant()
assert tenant.server_name == "acme.localhost"`,
    codeLang: "python",
    logLines: ["response built · tenant still bound"],
    upstream: "usynapse",
    upstreamFileRef: null,
    upstreamCode: `# Handler returns. There is nothing to "unbind" —
# upstream Synapse never bound a tenant in the
# first place.`,
    upstreamCodeLang: "python",
  },
  {
    id: "response_nginx",
    label: "Response · Synapse → Nginx",
    component: "nginx",
    layer: "response",
    startMs: 14400,
    endMs: 15800,
    fileRef: "docker-multitenant/nginx/nginx.conf",
    code: `# nginx receives the upstream response and forwards it
# back to the client unchanged. No tenant awareness here —
# that lived inside the Synapse process.
proxy_pass http://synapse:8008;`,
    codeLang: "nginx",
    logLines: ["nginx ← synapse 200 OK"],
    upstream: "unginx",
    upstreamFileRef: null,
    upstreamCode: `# Same as multi-tenant — nginx is just a
# transparent reverse proxy.
proxy_pass http://synapse:8008;`,
    upstreamCodeLang: "nginx",
  },
  {
    id: "response_client",
    label: "Response · Nginx → Client",
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
    if not registry.enabled:
        return [run_as_background_process(
            desc, hs.hostname, func, *args, **kwargs,
        )]

    deferreds = []
    for tenant in registry.get_all_tenants():
        async def _runner(_t=tenant):
            with tenant_context(_t):
                return await func(*args, **kwargs)
        deferreds.append(
            run_as_background_process(
                desc, tenant.server_name, _runner,
            )
        )
    return deferreds`,
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
# ever one tenant in the process, so the DB
# schema, keyring, and media root are whatever
# the global config picked at boot.
run_as_background_process(
    "user_directory.notify_new_event",
    hs.hostname,
    process,
)`,
    upstreamCodeLang: "python",
  },
  {
    id: "recap",
    label: "Isolation recap",
    component: "all",
    layer: "response",
    startMs: 19400,
    endMs: 21400,
    fileRef: null,
    code: `# Three tenants, one process, zero shared state:
#   acme.localhost    ->  tenant_acme
#   corp.localhost    ->  tenant_corp
#   startup.localhost ->  tenant_startup`,
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
];

export const TOTAL_DURATION_MS = STAGES[STAGES.length - 1].endMs;
