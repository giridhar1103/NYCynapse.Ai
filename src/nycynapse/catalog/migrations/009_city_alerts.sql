-- What is happening around the city right now, from the live feeds: road incidents and
-- closures (511NY), weather warnings (National Weather Service) and subway alerts tied to
-- stations (MTA). One row per alert, refreshed by the alerts poller every few minutes; an alert
-- the feed no longer carries is marked inactive. Area-wide alerts have no point.
CREATE TABLE app.city_alerts (
    id text PRIMARY KEY,                 -- source:feed id
    source text NOT NULL,                -- 511ny, nws, mta
    kind text NOT NULL,                  -- incident, closure, event, weather, subway
    title text NOT NULL,
    detail text,
    lat double precision,
    lon double precision,
    area text,
    severity integer NOT NULL DEFAULT 0, -- 0 info, 1 minor, 2 moderate, 3 severe
    starts timestamptz,
    ends timestamptz,
    first_seen timestamptz NOT NULL DEFAULT now(),
    last_seen timestamptz NOT NULL DEFAULT now(),
    active boolean NOT NULL DEFAULT true
);
CREATE INDEX city_alerts_active ON app.city_alerts (active, lat, lon);
CREATE INDEX city_alerts_first_seen ON app.city_alerts (first_seen);
