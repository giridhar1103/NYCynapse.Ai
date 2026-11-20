-- A reader can share one of their answers as a link. The link names the answer by a random id
-- of its own, not the conversation, and goes away with the turn after 30 days.
ALTER TABLE app.chat_turns ADD COLUMN share_id text UNIQUE;
