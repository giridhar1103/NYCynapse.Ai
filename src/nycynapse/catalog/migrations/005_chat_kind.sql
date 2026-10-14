-- Test conversations (from the evaluation runs) are kept apart from readers' conversations, so
-- they never count against readers' daily budget or rate limits.
ALTER TABLE app.chats ADD COLUMN kind text NOT NULL DEFAULT 'public';
