-- Multi-Tenant Synapse Database Initialization
-- Creates schemas for each tenant
-- Schema names match the TenantConfig.database_schema in homeserver.yaml

-- Create schemas for each tenant (matching homeserver.yaml configuration)
CREATE SCHEMA IF NOT EXISTS tenant_acme;
CREATE SCHEMA IF NOT EXISTS tenant_corp;
CREATE SCHEMA IF NOT EXISTS tenant_startup;

-- Grant permissions to synapse user
GRANT ALL ON SCHEMA tenant_acme TO synapse;
GRANT ALL ON SCHEMA tenant_corp TO synapse;
GRANT ALL ON SCHEMA tenant_startup TO synapse;

-- Also grant usage and create privileges for Synapse migrations
ALTER DEFAULT PRIVILEGES IN SCHEMA tenant_acme GRANT ALL ON TABLES TO synapse;
ALTER DEFAULT PRIVILEGES IN SCHEMA tenant_corp GRANT ALL ON TABLES TO synapse;
ALTER DEFAULT PRIVILEGES IN SCHEMA tenant_startup GRANT ALL ON TABLES TO synapse;

ALTER DEFAULT PRIVILEGES IN SCHEMA tenant_acme GRANT ALL ON SEQUENCES TO synapse;
ALTER DEFAULT PRIVILEGES IN SCHEMA tenant_corp GRANT ALL ON SEQUENCES TO synapse;
ALTER DEFAULT PRIVILEGES IN SCHEMA tenant_startup GRANT ALL ON SEQUENCES TO synapse;

-- Log success
DO $$
BEGIN
    RAISE NOTICE 'Multi-tenant schemas created successfully!';
    RAISE NOTICE '  - tenant_acme (for acme.localhost)';
    RAISE NOTICE '  - tenant_corp (for corp.localhost)';
    RAISE NOTICE '  - tenant_startup (for startup.localhost)';
END $$;
