-- Access to the public MCP server. People ask for access with a name, an email and what they
-- want it for; the owner approves or denies each one. An approved request gets a token, kept
-- here only as a hash. Usage is counted per token by the hour, for the limits.
CREATE TABLE app.access_requests (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    email text NOT NULL,
    purpose text NOT NULL,
    status text NOT NULL DEFAULT 'pending',   -- pending, approved, denied, revoked
    status_key_hash text NOT NULL,            -- the requester's private status link
    token_hash text UNIQUE,
    token_once text,                          -- shown once on the status page, then cleared
    token_hint text,
    per_hour integer NOT NULL DEFAULT 30,
    per_day integer NOT NULL DEFAULT 200,
    note text,
    client_hash text,
    created_at timestamptz NOT NULL DEFAULT now(),
    decided_at timestamptz,
    last_used_at timestamptz
);
CREATE INDEX access_requests_status ON app.access_requests (status, created_at);

CREATE TABLE app.access_usage (
    token_hash text NOT NULL,
    hour timestamptz NOT NULL,
    calls integer NOT NULL DEFAULT 0,
    PRIMARY KEY (token_hash, hour)
);
