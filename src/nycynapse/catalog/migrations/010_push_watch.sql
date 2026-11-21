-- Browsers that asked to be told about new alerts near a spot. The spot is rounded to about
-- 100 m and a watch lapses after 30 days unless the page renews it. The push endpoint and keys
-- are what the browser's push service needs to deliver a notification.
CREATE TABLE app.push_watch (
    endpoint text PRIMARY KEY,
    p256dh text NOT NULL,
    auth text NOT NULL,
    lat double precision NOT NULL,
    lon double precision NOT NULL,
    radius_m integer NOT NULL DEFAULT 1000,
    created_at timestamptz NOT NULL DEFAULT now(),
    renewed_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz,
    sent_today integer NOT NULL DEFAULT 0,
    sent_day date
);
