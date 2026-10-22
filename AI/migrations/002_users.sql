-- Accounts. Every conversation belongs to exactly one user, and so does every cost record.

CREATE TABLE users (
    id             TEXT PRIMARY KEY,
    -- Stored lowercase, and the CHECK makes the database enforce it, so "A@x.com" and
    -- "a@x.com" can never become two accounts.
    email          TEXT NOT NULL UNIQUE CHECK (email = lower(email)),
    -- An argon2id hash. Never the password itself.
    password_hash  TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL
);

ALTER TABLE conversations     ADD COLUMN user_id TEXT REFERENCES users(id) ON DELETE CASCADE;
-- Cost ledgers have NO foreign key on purpose: spend history must survive deleting a chat.
ALTER TABLE llm_calls         ADD COLUMN user_id TEXT;
ALTER TABLE archived_replies  ADD COLUMN user_id TEXT;

-- A database that already holds data from before accounts existed: give that data an owner
-- instead of losing it. 'legacy' cannot log in (its "hash" is not a valid hash); an admin
-- moves its data to a real account with:  python -m app.cli claim-legacy you@example.com
INSERT INTO users (id, email, password_hash, created_at)
SELECT 'legacy', 'legacy@streambot.invalid', '!', now()
WHERE EXISTS (SELECT 1 FROM conversations)
   OR EXISTS (SELECT 1 FROM llm_calls)
   OR EXISTS (SELECT 1 FROM archived_replies);

UPDATE conversations SET user_id = 'legacy' WHERE user_id IS NULL;
UPDATE llm_calls l SET user_id = c.user_id FROM conversations c WHERE c.id = l.conversation_id;
UPDATE llm_calls SET user_id = 'legacy' WHERE user_id IS NULL;
UPDATE archived_replies SET user_id = 'legacy' WHERE user_id IS NULL;

ALTER TABLE conversations    ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE llm_calls        ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE archived_replies ALTER COLUMN user_id SET NOT NULL;

-- "My conversations, most recent first" and "my spend since X" are the hot queries now.
DROP INDEX idx_conversations_updated;
CREATE INDEX idx_conversations_user_updated ON conversations (user_id, updated_at DESC, seq DESC);
CREATE INDEX idx_llm_calls_user_created ON llm_calls (user_id, created_at);
CREATE INDEX idx_archived_replies_user_created ON archived_replies (user_id, created_at);
