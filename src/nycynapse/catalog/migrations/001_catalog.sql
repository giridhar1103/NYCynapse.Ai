CREATE SCHEMA IF NOT EXISTS catalog;

CREATE TABLE catalog.versions (
    version     text PRIMARY KEY,
    loaded_at   timestamptz NOT NULL DEFAULT now(),
    git_sha     text,
    is_current  boolean NOT NULL DEFAULT false,
    stats       jsonb NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX versions_one_current ON catalog.versions (is_current) WHERE is_current;

-- Every semantic object, with the text that describes it and its embedding.
CREATE TABLE catalog.objects (
    version     text NOT NULL REFERENCES catalog.versions ON DELETE CASCADE,
    id          text NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('workspace', 'model', 'dimension', 'time',
                    'measure', 'metric', 'relationship', 'period', 'place', 'instruction')),
    workspace   text,
    model       text,
    name        text NOT NULL,
    label       text,
    body        text NOT NULL,
    payload     jsonb NOT NULL,
    embedding   vector(384),
    tsv         tsvector GENERATED ALWAYS AS (
                    to_tsvector('english', coalesce(label, '') || ' ' || body)) STORED,
    PRIMARY KEY (version, id)
);
CREATE INDEX objects_kind ON catalog.objects (version, kind);
CREATE INDEX objects_tsv ON catalog.objects USING gin (tsv);
CREATE INDEX objects_embedding ON catalog.objects USING hnsw (embedding vector_cosine_ops);

-- Distinct values of the dimensions that need grounding, for example complaint types.
CREATE TABLE catalog.dimension_values (
    version     text NOT NULL REFERENCES catalog.versions ON DELETE CASCADE,
    model       text NOT NULL,
    dimension   text NOT NULL,
    value       text NOT NULL,
    normalized  text NOT NULL,
    rows        bigint NOT NULL,
    PRIMARY KEY (version, model, dimension, value)
);
CREATE INDEX dimension_values_trgm ON catalog.dimension_values USING gin (normalized gin_trgm_ops);

-- What each named place covers, worked out from the lake's boundaries and station points.
CREATE TABLE catalog.place_coverage (
    version     text NOT NULL REFERENCES catalog.versions ON DELETE CASCADE,
    place       text NOT NULL,
    level       text NOT NULL,
    code        text NOT NULL,
    name        text,
    PRIMARY KEY (version, place, level, code)
);
