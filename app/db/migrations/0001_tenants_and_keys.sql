-- Tenants (teams or apps using the gateway) and their API keys.
-- Rules live in the database too, so bad data is rejected even if a code path forgets a check.

CREATE TABLE tenants (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Lowercase slug, e.g. "pipelinemedic". Used in logs and the admin CLI.
    name                text        NOT NULL UNIQUE
                                    CHECK (name ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
    requests_per_minute integer     NOT NULL CHECK (requests_per_minute > 0),
    daily_token_budget  bigint      NOT NULL CHECK (daily_token_budget > 0),
    created_at          timestamptz NOT NULL DEFAULT now(),
    disabled_at         timestamptz,
    CHECK (disabled_at IS NULL OR disabled_at >= created_at)
);

CREATE TABLE api_keys (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    -- RESTRICT: a tenant with keys can't be deleted by accident; disable it instead.
    tenant_id           uuid        NOT NULL REFERENCES tenants (id) ON DELETE RESTRICT,
    -- Public, non-secret part of the key, used to find the row without scanning.
    prefix              text        NOT NULL UNIQUE
                                    CHECK (prefix ~ '^[A-Za-z0-9]{8,32}$'),
    -- HMAC-SHA256 of the secret part (32 bytes). The key itself is never stored.
    key_hash            bytea       NOT NULL CHECK (octet_length(key_hash) = 32),
    -- Human label, e.g. "ci" or "laptop".
    name                text        NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
    -- Optional per-key limit, tighter than the tenant's.
    requests_per_minute integer     CHECK (requests_per_minute IS NULL OR requests_per_minute > 0),
    created_at          timestamptz NOT NULL DEFAULT now(),
    revoked_at          timestamptz,
    CHECK (revoked_at IS NULL OR revoked_at >= created_at)
);

CREATE INDEX api_keys_tenant_id_idx ON api_keys (tenant_id);
