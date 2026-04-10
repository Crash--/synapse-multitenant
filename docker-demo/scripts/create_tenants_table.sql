-- DDL for the public.tenants table used by database-sourced multi-tenant mode.
-- This table is the single source of truth for tenant definitions when
-- multi_tenant.source = "database" in homeserver.yaml.
--
-- The control plane service writes to this table; Synapse reads from it
-- at startup and on reload.

CREATE TABLE IF NOT EXISTS public.tenants (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    server_name                 TEXT NOT NULL UNIQUE,
    database_schema             TEXT NOT NULL UNIQUE,
    status                      TEXT NOT NULL DEFAULT 'provisioning'
                                CHECK (status IN ('provisioning', 'active', 'suspended', 'deleting')),

    -- Signing key (AES-256-GCM encrypted with SYNAPSE_TENANT_KEY_MASTER)
    signing_key_encrypted       BYTEA NOT NULL,
    signing_key_id              TEXT NOT NULL,

    -- Secrets
    macaroon_secret_key         TEXT,
    form_secret                 TEXT,
    registration_shared_secret  TEXT,

    -- Core config (typed, queryable, constrained)
    media_store_path            TEXT NOT NULL,
    registration_enabled        BOOLEAN NOT NULL DEFAULT false,
    enable_federation           BOOLEAN NOT NULL DEFAULT true,
    max_mau_value               INTEGER NOT NULL DEFAULT 0,
    public_baseurl              TEXT,
    server_notices_mxid         TEXT,
    trusted_key_servers         JSONB DEFAULT '[]'::jsonb,

    -- Complex sub-configs as JSONB (null = inherit global)
    email_config                JSONB,
    oidc_config                 JSONB,
    cas_config                  JSONB,
    saml_config                 JSONB,
    push_config                 JSONB,
    ratelimit_config            JSONB,
    app_service_config_files    JSONB,

    -- Audit
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    config_version              INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_tenants_status ON public.tenants(status);
