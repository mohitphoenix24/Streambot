---
title: Persistence: the database is the source of truth
level: easy
time: 35 min
summary: The SQLite schema and why it is shaped this way, how constraints protect the data, how old databases are upgraded in place, and how deleting a chat keeps the spend history true.
tags: SQLite, schema, foreign keys, constraints, migrations, NULL, cascade
---

Streambot keeps no important state in memory or in the browser. Every conversation, every message, every token count lives in one SQLite file, `AI/data/chat.db`. That is why a refresh or a server restart loses nothing, and why the history sent to the model is always rebuilt from stored rows.

## Why SQLite

SQLite is a full SQL database that lives in **one file**, with no server to install, and ships inside Python (`sqlite3`). For one user it is perfect: transactions, constraints, indexes, and zero setup. The honest limits are also worth knowing: one writer at a time, and a single file on one machine. A multi-user product would move to PostgreSQL; the *shape* of the schema would be the same.

## Two tables, not one

```python src="AI/app/db.py" fn="SCHEMA"
```

A conversation is *one thing* with a title; it owns *many* messages. If both lived in one table, the title would be repeated on every message, and "list my conversations" would have to scan every message. Two tables, linked by `messages.conversation_id`, avoid both problems.

Read the schema once more and notice what the database does **for you**, instead of hoping every code path remembers:

| Feature | What it enforces |
|---|---|
| `REFERENCES conversations(id) ON DELETE CASCADE` | a message cannot point at a chat that does not exist, and deleting a chat removes its messages |
| `CHECK (role IN ('user','assistant'))` | no message can have a made-up role |
| `CHECK (status IN ('streaming','complete','cancelled','error'))` | the reply state machine cannot hold an invalid state |
| `NOT NULL` on ids, titles, timestamps | the required facts are always present |
| Indexes on `(conversation_id, created_at)` and on `streaming` rows only | loading a chat and finding orphaned replies stay fast as the table grows |

## NULL means "unknown", never zero

:::def NULL
In SQL, `NULL` means "no value / unknown". It is different from `0` and from an empty string. Streambot uses it deliberately: `cost_usd` is `NULL` when no price is configured, and the UI shows "cost n/a". Writing `0.0` would be a lie, because the reply did cost something.
:::

Token columns work the same way: they hold what the provider *reported*, or `NULL` if it reported nothing (for example a reply that was stopped before the final event). Sums skip `NULL`s, so nothing is ever invented.

## Break it on purpose

Here is the app's real schema, running in an in-memory database in your browser. Try the experiments, then add your own.

```python run include="AI/app/db.py:SCHEMA" title="The real schema: constraints in action" level=easy
import sqlite3

conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA foreign_keys = ON")      # SQLite has this OFF by default, PER CONNECTION
conn.executescript(SCHEMA)

conn.execute("INSERT INTO conversations (id, title, created_at, updated_at) VALUES ('c1', 'Demo', 'now', 'now')")
conn.execute("INSERT INTO messages (id, conversation_id, role, content, status, created_at) "
             "VALUES ('m1', 'c1', 'user', 'hello', 'complete', 'now')")
conn.execute("INSERT INTO messages (id, conversation_id, role, content, status, cost_usd, created_at) "
             "VALUES ('m2', 'c1', 'assistant', 'hi!', 'complete', NULL, 'now')")

def attempt(label, sql):
    try:
        conn.execute(sql)
        print(f"allowed   : {label}")
    except sqlite3.IntegrityError as e:
        print(f"REFUSED   : {label}  ({e})")

attempt("a message for a chat that does not exist",
        "INSERT INTO messages (id, conversation_id, role, content, status, created_at) VALUES ('x', 'nope', 'user', '', 'complete', 'now')")
attempt("a made-up role",
        "INSERT INTO messages (id, conversation_id, role, content, status, created_at) VALUES ('y', 'c1', 'robot', '', 'complete', 'now')")
attempt("a made-up status",
        "INSERT INTO messages (id, conversation_id, role, content, status, created_at) VALUES ('z', 'c1', 'user', '', 'sleeping', 'now')")

# NULL cost is "unknown", and SUM() skips it instead of treating it as zero:
print("cost of m2:", conn.execute("SELECT cost_usd FROM messages WHERE id = 'm2'").fetchone()[0])
print("total cost:", conn.execute("SELECT SUM(cost_usd) FROM messages").fetchone()[0])

# Deleting the conversation removes its messages (ON DELETE CASCADE):
conn.execute("DELETE FROM conversations WHERE id = 'c1'")
print("messages left:", conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
```

Two lessons hide in that demo. First, **foreign-key enforcement is off by default in SQLite and is a per-connection setting**, so the app switches it on every time it opens a connection. Second, the database itself refuses bad data; you do not need to trust every code path.

## Short-lived connections

```python src="AI/app/db.py" fn="Database.connect"
```

Every method opens a connection, does one small thing, commits and closes. Each SQLite call is a sub-millisecond single-row operation, so running them directly inside async code is fine for this project (the upgrade path is `aiosqlite` or a thread pool). **WAL mode** (switched on in `init_schema`) lets readers continue while a writer is active, which matters while a reply is being checkpointed during a read.

## Upgrading old databases in place

`CREATE TABLE IF NOT EXISTS` never changes a table that already exists. Streambot gained columns over time (cached tokens, sources, summaries…), and people already had database files. The fix is small and additive:

```python src="AI/app/db.py" fn="_ADDED_COLUMNS,Database._migrate"
```

On every startup it compares the table's real columns with the list, and `ALTER TABLE … ADD COLUMN` for each missing one. Old data stays intact and the new columns are `NULL`. There are tests that build a deliberately old database file and check it upgrades.

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

Deleting a chat removes its content. But the tokens were spent either way, so deleting a chat must not make your total spend shrink. The fix: before the cascade delete, copy each reply's **numbers** (never its text) into a separate table, in the same transaction.

```python src="AI/app/db.py" fn="Database.delete_conversation"
```

`INSERT OR IGNORE` makes a repeated delete harmless, and the original timestamp is kept so the spend stays on the day it really happened. The Usage page reads this table too (see the caching lesson).

## Conversation titles and ordering

Two small details. A new chat gets a *provisional* title instantly (the first words of your message, free), and a better one from a small model call after the first completed reply. And messages are ordered by `created_at, rowid`: a user message and its assistant placeholder are created in the same millisecond, so insertion order (`rowid`) breaks the tie.

## What the API returns

Rows are turned into typed response models by Pydantic. One small validator is worth reading: sources are stored as a JSON string in SQLite, and decoded into a list at the edge.

```python src="AI/app/schemas.py" fn="MessageOut"
```

:::keypoints
- The database is the **source of truth**; nothing important lives only in memory or the browser.
- Two tables, a **foreign key** with `ON DELETE CASCADE`, and **CHECK constraints** let the database refuse bad data itself.
- `NULL` means *unknown*, never zero. Sums skip it, and unpriced replies are counted separately.
- Foreign keys must be switched on **per connection** in SQLite.
- Old databases are upgraded **additively** (`ALTER TABLE … ADD COLUMN`) on every startup.
- Bind values as parameters; **whitelist** anything that cannot be bound, like column names.
- Deleting a chat **archives the usage numbers** so spend history stays true, while the content is really deleted.
:::

:::exercise Add a constraint
In the demo above, add a check that no assistant message may have a negative `cost_usd`. What SQL would you add to the `messages` table definition, and how would an old database file get it?
:::

:::solution Show the answer
Add `CHECK (cost_usd IS NULL OR cost_usd >= 0)` to the column in the `CREATE TABLE`. For old files, `ALTER TABLE` cannot add a CHECK to an existing column in SQLite, so you would create a new table with the constraint, copy the rows across, drop the old table and rename the new one (a "table rebuild"). That is why additive columns are easy and constraints on existing columns are not.
:::

```quiz
? Why does `cost_usd` hold NULL instead of 0 when no price is configured?
- NULL takes less space.
+ 0 would claim the reply was free; NULL honestly means "unknown", and sums skip it.
- SQLite does not allow 0 in a REAL column.
> A wrong number in a cost display is worse than an honest blank.

? What does `ON DELETE CASCADE` on `messages.conversation_id` do?
- Prevents deleting a conversation that has messages.
+ Deletes a conversation's messages automatically when the conversation is deleted.
- Moves the messages to another conversation.
> The database keeps its own integrity. Streambot also copies each reply's usage numbers to an archive table first.

? Why does the app whitelist the columns `update_message` may change?
- Because SQLite is case sensitive.
+ Column names cannot be bound as SQL parameters, so unchecked names would be an injection risk.
- To make updates faster.
> Values are bound; names are checked against `_UPDATABLE` before any SQL is built.
```
