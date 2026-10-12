-- Conversations with the guide agent. Kept private for 30 days to review how the models answer,
-- then deleted by the daily purge. Nothing here is shown to other readers.
CREATE TABLE app.chats (
    id           uuid PRIMARY KEY,
    started_at   timestamptz NOT NULL DEFAULT now(),
    last_at      timestamptz NOT NULL DEFAULT now(),
    client_hash  text,
    turns        integer NOT NULL DEFAULT 0
);
CREATE INDEX chats_started ON app.chats (started_at);
CREATE INDEX chats_client ON app.chats (client_hash, started_at DESC);

CREATE TABLE app.chat_turns (
    id           uuid PRIMARY KEY,
    chat_id      uuid NOT NULL REFERENCES app.chats (id) ON DELETE CASCADE,
    turn         integer NOT NULL,
    asked_at     timestamptz NOT NULL DEFAULT now(),
    question     text NOT NULL,
    answer       text,
    outcome      text,          -- answered, declined, failed, busy
    model        text,          -- the provider that wrote the answer
    fell_back    boolean NOT NULL DEFAULT false,
    steps        jsonb NOT NULL DEFAULT '[]',
    sources      jsonb NOT NULL DEFAULT '[]',
    map          jsonb NOT NULL DEFAULT '[]',
    memory       jsonb,         -- what the next turn is told about this one
    model_calls  integer,
    cost_usd     double precision,
    latency_ms   integer,
    error        text,
    UNIQUE (chat_id, turn)
);
CREATE INDEX chat_turns_asked ON app.chat_turns (asked_at);
