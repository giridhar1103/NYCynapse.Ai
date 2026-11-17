-- A reader can mark an answer as right or wrong, with a short note. Kept with the turn, so the
-- 30 day cleanup removes it too.
ALTER TABLE app.chat_turns
    ADD COLUMN rating smallint,
    ADD COLUMN rating_note text,
    ADD COLUMN rated_at timestamptz;
