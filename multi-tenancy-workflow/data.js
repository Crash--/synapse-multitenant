// Single source of narrative truth.
// Each stage has start/end ms (relative to playback start), label,
// fileRef (real repo path), code (Python snippet), and logLines.

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
  },
  {
    id: "response",
    label: "Response",
    component: "client",
    layer: "response",
    startMs: 13000,
    endMs: 14500,
    fileRef: null,
    code: `HTTP/1.1 200 OK
Content-Type: application/json

{ "versions": ["v1.13", "v1.14"] }`,
    codeLang: "http",
    logLines: ["200 OK"],
  },
  {
    id: "recap",
    label: "Isolation recap",
    component: "all",
    layer: "response",
    startMs: 14500,
    endMs: 16500,
    fileRef: null,
    code: `# Three tenants, one process, zero shared state:
#   acme.localhost    ->  tenant_acme
#   corp.localhost    ->  tenant_corp
#   startup.localhost ->  tenant_startup`,
    codeLang: "python",
    logLines: ["all tenants isolated · 1 process"],
  },
];

export const TOTAL_DURATION_MS = STAGES[STAGES.length - 1].endMs;
