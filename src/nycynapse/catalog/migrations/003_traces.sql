-- Every question asked through the API, with what each stage did. Lets any answer be replayed
-- and explained later.
CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE app.traces (
    id              uuid PRIMARY KEY,
    asked_at        timestamptz NOT NULL DEFAULT now(),
    question        text NOT NULL,
    as_of           text NOT NULL,
    classification  text,
    workspaces      text[],
    sql             text,
    answer          text,
    result          jsonb,
    stages          jsonb NOT NULL DEFAULT '[]',
    catalog_version text,
    snapshot_id     bigint,
    model_calls     integer,
    cost_usd        double precision,
    latency_ms      integer,
    client_hash     text,
    error           text
);
CREATE INDEX traces_asked_at ON app.traces (asked_at DESC);
CREATE INDEX traces_client ON app.traces (client_hash, asked_at DESC);
