-- Per-chat attachments and retrieval (RAG): documents, their chunks, and vector + keyword search.

CREATE EXTENSION IF NOT EXISTS vector;

-- One row per uploaded file. A document belongs to ONE chat (and so to one user); deleting the
-- chat deletes its documents, and deleting a document deletes its chunks, by cascade.
CREATE TABLE documents (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    filename        TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('pdf', 'docx', 'text', 'markdown')),
    size_bytes      INTEGER NOT NULL,
    -- A document is processed in the background (extract text, split, embed), so it has a state:
    status          TEXT NOT NULL CHECK (status IN ('processing', 'ready', 'failed')),
    error           TEXT,               -- a safe, short reason when status = 'failed'
    page_count      INTEGER,
    chunk_count     INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL
);
CREATE INDEX idx_documents_conversation ON documents (conversation_id, created_at);

-- A chunk is a passage of a document (~500 tokens): the unit that is embedded and retrieved.
CREATE TABLE chunks (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id     TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    -- user_id and conversation_id are copied from the document on purpose: every search filters
    -- on them directly, so one user's query can never touch another user's chunks, even by a bug
    -- in a join.
    user_id         TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    ordinal         INTEGER NOT NULL,   -- position in the document
    page            INTEGER,            -- PDF page (NULL for formats without pages)
    heading         TEXT,               -- the nearest heading above this passage, if any
    content         TEXT NOT NULL,
    tokens          INTEGER NOT NULL,   -- estimated size, for budgeting
    -- The embedding: 1536 numbers describing the passage's MEANING (OpenAI text-embedding-3-small).
    -- Passages with similar meaning have vectors that point in similar directions.
    embedding       vector(1536) NOT NULL,
    -- The same passage prepared for keyword search (stemmed words), maintained by Postgres itself.
    tsv             tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
);

-- Approximate nearest-neighbour index for vector search (HNSW, cosine distance).
CREATE INDEX idx_chunks_embedding ON chunks USING hnsw (embedding vector_cosine_ops);
-- Inverted index for keyword search (exact names, codes, rare words that meaning-search can miss).
CREATE INDEX idx_chunks_tsv ON chunks USING gin (tsv);
-- Every search starts by narrowing to one user's one chat.
CREATE INDEX idx_chunks_scope ON chunks (conversation_id, user_id);

-- Embedding calls (indexing a document, embedding a question) cost money too.
ALTER TABLE llm_calls DROP CONSTRAINT llm_calls_purpose_check;
ALTER TABLE llm_calls ADD CONSTRAINT llm_calls_purpose_check CHECK (purpose IN ('title', 'summary', 'embedding'));
