/**
 * Build the per-tenant oidc_config JSON and PATCH it into public.tenants
 * via the control plane API. Called after LDAP seeding.
 *
 * NOT a "use server" module — imported by the stream route, which is
 * already server-side. Marking as Server Actions would force every
 * export to be async, but buildTenantOidcConfig is pure sync.
 */

const CONTROL_PLANE_URL = process.env.CONTROL_PLANE_URL ?? "http://control-plane:3001";
const CONTROL_PLANE_TOKEN = process.env.CONTROL_PLANE_TOKEN ?? "";
const OIDC_CLIENT_ID = process.env.OIDC_CLIENT_ID ?? "synapse-demo";
const OIDC_CLIENT_SECRET = process.env.OIDC_CLIENT_SECRET ?? "demo-secret-change-in-prod";
const OIDC_ISSUER = process.env.OIDC_ISSUER ?? "https://lemonldap.localhost/";
const OIDC_PROXY = process.env.OIDC_PROXY ?? "https://oidc-proxy.localhost";

export function buildTenantOidcConfig(_serverName: string): Record<string, unknown> {
  return {
    enabled: true,
    providers: [
      {
        idp_id: "lemonldap",
        idp_name: "LemonLDAP SSO",
        discover: false,
        issuer: OIDC_ISSUER,
        authorization_endpoint: `${OIDC_PROXY}/api/oidc/authorize`,
        token_endpoint: `${OIDC_PROXY}/api/oidc/token`,
        userinfo_endpoint: `${OIDC_PROXY}/api/oidc/userinfo`,
        jwks_uri: `${OIDC_PROXY}/api/oidc/jwks`,
        client_id: OIDC_CLIENT_ID,
        client_secret: OIDC_CLIENT_SECRET,
        client_auth_method: "client_secret_basic",
        scopes: ["openid", "email", "profile"],
        skip_verification: true,
        allow_existing_users: true,
        enable_registration: true,
        user_mapping_provider: {
          config: {
            subject_claim: "sub",
            localpart_template: "{{ user.sub.split('@')[0] }}",
            display_name_template: "{{ user.sub.split('@')[0] }}",
            email_template: "{{ user.sub }}",
          },
        },
      },
    ],
  };
}

export async function applyTenantOidcConfig(serverName: string): Promise<void> {
  const oidc_config = buildTenantOidcConfig(serverName);
  const resp = await fetch(
    `${CONTROL_PLANE_URL}/api/v1/tenants/${encodeURIComponent(serverName)}`,
    {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${CONTROL_PLANE_TOKEN}`,
      },
      body: JSON.stringify({ oidc_config }),
    }
  );
  if (!resp.ok) {
    throw new Error(
      `PATCH /tenants/${serverName} failed: ${resp.status} ${await resp.text()}`
    );
  }
}
