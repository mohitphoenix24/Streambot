-- Streambot, first schema: conversations, messages, and the usage ledgers.
--
-- Written once, never edited after it ships: later changes are NEW numbered files
-- (002_..., 003_...), applied in order by app/migrations.py and recorded in
-- schema_migrations. That is how every database, old or new, ends up identical.

CREATE TABLE conversations (
    -- seq: insertion order. Two rows created in the same millisecond must still sort
    -- deterministically, and Postgres has no hidden rowid like SQLite.
    seq         BIGINT GENERATED ALWAYS AS IDENTITY,
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL,
    -- Rolling memory: a summary of the oldest messages, and the id of the last message
    -- it covers. Everything after that message is still sent verbatim.
    summary             TEXT,
    summary_through_id  TEXT
);

CREATE TABLE messages (
    seq               BIGINT GENERATED ALWAYS AS IDENTITY,
    id                TEXT PRIMARY KEY,
    conversation_id   TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role              TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content           TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL CHECK (status IN ('streaming', 'complete', 'cancelled', 'error')),
    -- Usage REPORTED by the provider (NULL = unavailable; never estimated):
    input_tokens        INTEGER,   -- includes the cached part
    cached_input_tokens INTEGER,   -- subset of input_tokens, billed at the cached rate
    output_tokens       INTEGER,   -- includes reasoning tokens
    reasoning_tokens    INTEGER,   -- subset of output_tokens (observability only)
    total_tokens        INTEGER,
    cost_usd            DOUBLE PRECISION,  -- usage x prices.toml; NULL = unpriced/unknown
    stop_reason         TEXT,
    model               TEXT,      -- REQUESTED model (from OPENAI_MODEL)
    served_model        TEXT,      -- model id OpenAI reports it actually served (may be a snapshot)
    ttft_ms             DOUBLE PRECISION,  -- time to first token (NULL if none arrived)
    latency_ms          DOUBLE PRECISION,  -- total time until the reply finished
    sources             TEXT,      -- JSON list of web sources cited by this reply (NULL = none)
    web_searches        INTEGER,   -- how many web searches this reply ran
    created_at          TIMESTAMPTZ NOT NULL
);

-- Loading a conversation = "all messages for conversation X, oldest first".
CREATE INDEX idx_messages_conversation ON messages (conversation_id, seq);

-- Sidebar = "conversations, most recently active first".
CREATE INDEX idx_conversations_updated ON conversations (updated_at DESC, seq DESC);

-- Tiny partial index: only 'streaming' rows are indexed, so crash recovery
-- ("which replies were in flight?") is instant however big the table gets.
CREATE INDEX idx_messages_streaming ON messages (status) WHERE status = 'streaming';

-- Model calls that are not a chat reply (titles, summaries), so their cost is not lost.
-- No foreign key on purpose: spend history must survive deleting the conversation.
CREATE TABLE llm_calls (
    id                  TEXT PRIMARY KEY,
    conversation_id     TEXT,
    purpose             TEXT NOT NULL CHECK (purpose IN ('title', 'summary')),
    model               TEXT NOT NULL,
    served_model        TEXT,
    input_tokens        INTEGER,
    cached_input_tokens INTEGER,
    output_tokens       INTEGER,
    reasoning_tokens    INTEGER,
    total_tokens        INTEGER,
    cost_usd            DOUBLE PRECISION,
    created_at          TIMESTAMPTZ NOT NULL
);
CREATE INDEX idx_llm_calls_created ON llm_calls (created_at);

-- Usage of replies whose chat was deleted. Deleting a chat removes its CONTENT, but the
-- tokens were spent either way, so each reply's numbers (never its text) are kept here.
CREATE TABLE archived_replies (
    id                  TEXT PRIMARY KEY,   -- the original message id
    conversation_id     TEXT,
    model               TEXT,
    served_model        TEXT,
    input_tokens        INTEGER,
    cached_input_tokens INTEGER,
    output_tokens       INTEGER,
    reasoning_tokens    INTEGER,
    total_tokens        INTEGER,
    cost_usd            DOUBLE PRECISION,
    web_searches        INTEGER,
    created_at          TIMESTAMPTZ NOT NULL,  -- when the reply happened (keeps it on the right day)
    archived_at         TIMESTAMPTZ NOT NULL
);
CREATE INDEX idx_archived_replies_created ON archived_replies (created_at);
