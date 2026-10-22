---
title: Persistence: PostgreSQL, constraints and migrations
level: medium
time: 40 min
summary: Why the database is the source of truth, how the schema is shaped and protected by constraints, how numbered migrations keep every database identical, and how deleting a chat keeps the spend history true.
tags: PostgreSQL, schema, foreign keys, constraints, migrations, connection pool, NULL, cascade
---

Streambot keeps no important state in memory or in the browser. Every account, conversation, message, token count and document lives in PostgreSQL. That is why a refresh or a server restart loses nothing, and why the history sent to the model is always rebuilt from stored rows.

## Why PostgreSQL

The first version of Streambot used SQLite: one file, no server, perfect for one user. Once the app had **accounts** and **document search** the requirements changed:

| Need | Why PostgreSQL fits |
|---|---|
| Many people writing at once | SQLite allows one writer at a time; Postgres is a server built for concurrent writers |
| Rules the database enforces | foreign keys, `CHECK` constraints and cascades work the same everywhere, with no per-connection switches |
| Changing the schema safely | real transactional DDL: a failed migration rolls back completely |
| Vector search | the `pgvector` extension stores embeddings and searches them with an index, *next to* the rest of the data (see the RAG lessons) |

The database runs in a container (`docker-compose.yml`, image `pgvector/pgvector:pg16`) and `./run.sh` starts it for you.

## Two tables, not one (and more around them)

The core idea has not changed. A conversation is *one thing* with a title; it owns *many* messages. If both lived in one table the title would repeat on every message and "list my conversations" would have to scan every message.

```sql src="AI/migrations/001_initial.sql" lines="7-46"
```

Notice what the database does **for you**, instead of hoping every code path remembers:

| Feature | What it enforces |
|---|---|
| `REFERENCES conversations(id) ON DELETE CASCADE` | a message cannot point at a chat that does not exist, and deleting a chat removes its messages |
| `CHECK (role IN ('user','assistant'))` | no message can have a made-up role |
| `CHECK (status IN (...))` | the reply state machine cannot hold an invalid state |
| `NOT NULL` on ids, titles, timestamps | the required facts are always present |
| `seq BIGINT GENERATED ALWAYS AS IDENTITY` | insertion order, so two rows created in the same millisecond still sort correctly (Postgres has no hidden `rowid`) |
| A partial index on `status = 'streaming'` | finding orphaned replies after a crash stays fast |

Later migrations added `users` (every conversation, document and cost row has an owner), and `documents` and `chunks` for attachments.

## NULL means "unknown", never zero

:::def NULL
In SQL, `NULL` means "no value / unknown". It is different from `0` and from an empty string. Streambot uses it deliberately: `cost_usd` is `NULL` when no price is configured, and the UI shows "cost n/a". Writing `0.0` would be a lie, because the reply did cost something.
:::

Token columns work the same way: they hold what the provider *reported*, or `NULL` if it reported nothing (for example a reply that was stopped before the final event). Sums skip `NULL`s, so nothing is ever invented.

## Break it on purpose

Postgres cannot run inside a web page, so this demo uses SQLite, which behaves the same for these rules. It is a **miniature of the real schema**: the same table shapes, constraints and cascade. Try the experiments, then add your own.

```python run title="A miniature of the schema: constraints in action" level=easy
import sqlite3

conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA foreign_keys = ON")      # SQLite needs this per connection; Postgres always enforces them
conn.executescript("""
CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL);
CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role   TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    status TEXT NOT NULL CHECK (status IN ('streaming', 'complete', 'cancelled', 'error')),
    content TEXT NOT NULL DEFAULT '',
    cost_usd REAL
);
""")

conn.execute("INSERT INTO conversations VALUES ('c1', 'Demo')")
conn.execute("INSERT INTO messages (id, conversation_id, role, status, content) VALUES ('m1', 'c1', 'user', 'complete', 'hello')")
conn.execute("INSERT INTO messages (id, conversation_id, role, status, content, cost_usd) VALUES ('m2', 'c1', 'assistant', 'complete', 'hi!', NULL)")

def attempt(label, sql):
    try:
        conn.execute(sql)
        print(f"allowed   : {label}")
    except sqlite3.IntegrityError as e:
        print(f"REFUSED   : {label}  ({e})")

attempt("a message for a chat that does not exist",
        "INSERT INTO messages (id, conversation_id, role, status) VALUES ('x', 'nope', 'user', 'complete')")
attempt("a made-up role",
        "INSERT INTO messages (id, conversation_id, role, status) VALUES ('y', 'c1', 'robot', 'complete')")
attempt("a made-up status",
        "INSERT INTO messages (id, conversation_id, role, status) VALUES ('z', 'c1', 'user', 'sleeping')")

# NULL cost is "unknown", and SUM() skips it instead of treating it as zero:
print("cost of m2:", conn.execute("SELECT cost_usd FROM messages WHERE id = 'm2'").fetchone()[0])
print("total cost:", conn.execute("SELECT SUM(cost_usd) FROM messages").fetchone()[0])

# Deleting the conversation removes its messages (ON DELETE CASCADE):
conn.execute("DELETE FROM conversations WHERE id = 'c1'")
print("messages left:", conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
```

The database itself refuses bad data; you do not need to trust every code path. In the real app the tests run against a real PostgreSQL (one throwaway database per test), so these rules are tested on the engine that enforces them.

## Migrations: how every database ends up identical

A schema changes over time: new columns, new tables. Your laptop, a teammate's and production must all end up with the **same** schema. The standard answer is **migrations**: numbered SQL files, applied in order, each exactly once.

```text
AI/migrations/001_initial.sql      conversations, messages, usage ledgers
AI/migrations/002_users.sql        accounts; every chat and cost row gets an owner
AI/migrations/003_documents.sql    documents and chunks (pgvector)
```

```python src="AI/app/migrations.py" fn="migrate"
```

Read it slowly, because every line is a rule:

- **`schema_migrations`** records which versions a database already has, so running this at every startup is safe: it applies only what is missing.
- **One transaction.** All pending files run together; Postgres can roll back schema changes, so a failure leaves the database exactly as it was (there is a test for it).
- **An advisory lock** serialises concurrent startups: two server processes booting at once must not both apply the same migration.
- **A migration is never edited after it ships.** A change is a *new* file. That is the whole discipline.

`002_users.sql` shows why migrations matter. A database that already held chats from before accounts existed needs those chats to have an owner, so the migration creates a placeholder `legacy` account, assigns the old rows to it, and only *then* makes `user_id` `NOT NULL`:

```sql src="AI/migrations/002_users.sql" from="INSERT INTO users" to="ALTER TABLE archived_replies ALTER COLUMN user_id SET NOT NULL"
```

An admin later moves that data to a real account with `python -m app.cli claim-legacy you@example.com`, or imports the old SQLite file with `import-sqlite`.

:::def Alembic
A popular Python migration framework. Streambot's runner is about 40 lines instead, because the idea is the same and nothing is hidden. For a large team you would reach for Alembic (or a similar tool) to get autogenerated migrations and branching.
:::

## Connections come from a pool

```python src="AI/app/db.py" fn="Database.connect"
```

Opening a Postgres connection costs milliseconds, so connections are **pooled** and reused. Every method borrows one, does one small thing, and returns it; the borrow commits on success and rolls back on any error.

The calls are **synchronous on purpose**. They take well under a millisecond on a local database, and keeping them synchronous preserves something the streaming code relies on: the final `finally` block that saves a reply contains **no `await`**, so a cancelled task cannot be interrupted halfway through saving. The cost is that a very slow database would stall the event loop; the upgrade path is psycopg's async pool plus a shielded save.

## Updating a row safely

SQL *values* are always bound as parameters (never pasted into the query), which defeats SQL injection. But a **column name** cannot be a bound parameter. `update_message(**fields)` builds the column list dynamically, so it is guarded by a whitelist:

```python src="AI/app/db.py" fn="_UPDATABLE,Database.update_message"
```

:::warn Where injection hides
The classic mistake is to bind the values but format the column names in from user input. Here the names come only from our own code, and anything not in `_UPDATABLE` raises an error before any SQL runs.
:::

## Totals are sums of what was recorded

```python src="AI/app/db.py" fn="Database.conversation_totals"
```

Conversation cost is just `SUM(cost_usd)` over its rows, and unpriced replies are *counted separately* so the UI can say "$0.012 + 2 unpriced" instead of pretending the total is complete.

## Deleting a chat without rewriting history

Deleting a chat removes its content (messages, documents and their chunks, by cascade). But the tokens were spent either way, so deleting a chat must not make your total spend shrink. The fix: before the cascade delete, copy each reply's **numbers** (never its text) into a separate table, in the same transaction.

```python src="AI/app/db.py" fn="Database.delete_conversation"
```

`ON CONFLICT (id) DO NOTHING` makes a repeated delete harmless, and the original timestamp is kept so the spend stays on the day it really happened. Note the `user_id` in the `WHERE`: you can only delete (and archive) **your own** chat. The Usage page reads this table too (see the caching lesson).

## Titles, ordering and timestamps

A new chat gets a *provisional* title instantly (the first words of your message, free), and a better one from a small model call after the first completed reply. Messages are ordered by `seq`, the identity column, for the reason in the table above. Timestamps are stored as `TIMESTAMPTZ` and returned to the API as ISO-8601 strings, so the browser sees the same format as before.

## What the API returns

Rows are turned into typed response models by Pydantic. One small validator is worth reading: sources are stored as a JSON string, and decoded into a list at the edge.

```python src="AI/app/schemas.py" fn="MessageOut"
```

:::keypoints
- The database is the **source of truth**; nothing important lives only in memory or the browser.
- Two core tables, a **foreign key** with `ON DELETE CASCADE`, and **CHECK constraints** let the database refuse bad data itself.
- `NULL` means *unknown*, never zero. Sums skip it, and unpriced replies are counted separately.
- The schema is built by **numbered migrations**, applied once each, in one transaction, under a lock. Never edit a shipped migration; add a new one.
- Connections are **pooled**; the calls are synchronous so the "save on the way out" block has no `await`.
- Bind values as parameters; **whitelist** anything that cannot be bound, like column names.
- Deleting a chat **archives the usage numbers** so spend history stays true, while the content is really deleted.
:::

:::exercise Add a constraint
Add a rule that no assistant message may have a negative `cost_usd`. Which file would you create, what SQL goes in it, and what happens on a database that already contains data?
:::

:::solution Show the answer
Create `004_cost_check.sql` containing `ALTER TABLE messages ADD CONSTRAINT messages_cost_nonnegative CHECK (cost_usd IS NULL OR cost_usd >= 0);`. You do **not** edit `001_initial.sql`: it has already shipped, and editing it would leave databases that applied the old version different from new ones. Postgres checks existing rows when the constraint is added, so if any old row is negative the migration fails, rolls back completely, and tells you which data to fix first. (SQLite cannot add a CHECK to an existing table at all, which is one reason to prefer Postgres here.)
:::

```quiz
? Why does `cost_usd` hold NULL instead of 0 when no price is configured?
- NULL takes less space.
+ 0 would claim the reply was free; NULL honestly means "unknown", and sums skip it.
- Postgres does not allow 0 in a double precision column.
> A wrong number in a cost display is worse than an honest blank.

? Why is a shipped migration never edited?
- Because the files are read-only.
+ Databases that already applied it would differ from new ones; a change must be a new numbered file.
- Because Postgres forbids rewriting history.
> The whole point of migrations is that every database ends up with the same schema.

? What does `ON DELETE CASCADE` on `messages.conversation_id` do?
- Prevents deleting a conversation that has messages.
+ Deletes a conversation's messages automatically when the conversation is deleted.
- Moves the messages to another conversation.
> The database keeps its own integrity. Streambot also copies each reply's usage numbers to an archive table first.

? Why does the app whitelist the columns `update_message` may change?
- Because Postgres is case sensitive.
+ Column names cannot be bound as SQL parameters, so unchecked names would be an injection risk.
- To make updates faster.
> Values are bound; names are checked against `_UPDATABLE` before any SQL is built.
```
