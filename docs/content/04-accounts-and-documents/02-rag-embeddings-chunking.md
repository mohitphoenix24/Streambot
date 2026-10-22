---
title: RAG: embeddings, chunks and pgvector
level: hard
time: 60 min
summary: Why you retrieve instead of pasting a whole document, what an embedding is, how Streambot splits files into passages, turns them into vectors with OpenAI, and stores them in PostgreSQL with pgvector.
tags: RAG, embeddings, vectors, cosine similarity, chunking, overlap, pgvector, HNSW, cost
---

You can attach a 200-page PDF to a chat and ask "what is the notice period?". The model cannot simply read the whole file: it would not fit in the context window, and even if it did, you would pay for every page on every question. **RAG (retrieval-augmented generation)** is the standard answer: *find the few passages that matter, and give the model only those.*

:::flow The RAG pipeline, in two halves
Index (once) | read the file → split into passages → turn each into a vector → store them
Ask | the model turns your question into a search query → that is turned into a vector too
Retrieve | find the stored passages whose vectors are nearest to the question's vector
Answer | the model reads only those passages and answers, citing file and page
:::

This lesson covers the **indexing** half. The next lesson covers retrieval, and the one after it the upload pipeline around both.

## What an embedding is

:::def Embedding
A list of numbers (a *vector*) that represents the **meaning** of a piece of text. An embedding model reads a passage and returns, for `text-embedding-3-small`, **1,536 numbers**: a point in a 1,536-dimensional space. Passages with similar meaning land **close together**; unrelated ones land far apart.
:::

The payoff: *"How do I cancel my subscription?"* sits near *"Steps to terminate your plan"* even though the two share no words. Searching by meaning becomes plain geometry: find the stored points nearest to the question's point.

"Near" is measured with **cosine similarity**: the angle between two vectors. 1.0 means the same direction (same meaning), around 0 means unrelated.

Streambot's tests need embeddings without calling OpenAI, so they use a deterministic stand-in: each word is hashed to one of 1,536 slots and the vector is normalised. It has *no real meaning*, but it behaves like an embedding in one way: texts that share words get a high similarity. That is enough to test the whole pipeline, and it makes a handy demo (and a reminder of what the real model adds):

```python run include="AI/app/rag/embeddings.py:EMBEDDING_DIMENSIONS|AI/tests/helpers.py:fake_vector" title="Cosine similarity with the test stand-in for an embedding" level=medium
import hashlib, math

def cosine(a, b):
    return sum(x * y for x, y in zip(a, b))        # vectors have length 1, so the dot product IS the cosine

question = "How long do refunds take?"
passages = [
    "Refunds are issued within five business days.",
    "Our office is closed on public holidays and weekends.",
    "Shipping to remote islands may take three extra weeks.",
    "The money-back guarantee is processed within a week.",     # same MEANING as the question, almost no shared words
]
q = fake_vector(question)
print(len(q), "numbers per vector\n")
for text in passages:
    print(f"{cosine(q, fake_vector(text)):.3f}  {text}")

print("\nThe stand-in only sees shared WORDS, so it misses the last passage even though it answers the question.")
print("A real embedding model places 'money-back guarantee ... within a week' near 'refunds take how long'.")
print("That is what the 1,536 learned numbers buy you.")
```

## Step 1: read the file

Extraction (PDF, Word, text, Markdown) is its own lesson. What matters here is the *shape* it produces: a list of **pages**, each with a number (or `None` for formats without pages) and its text. Word headings become Markdown `#` lines, so Word and Markdown are chunked the same way.

## Step 2: chunking

Why not embed the whole document as one vector? One vector is one *summary*: embed a 200-page PDF and it means "kind of about everything". Embed 450-token passages and each vector means something **specific**, so a question can find the exact passage that answers it. But too-small chunks lose their context ("it takes five days": what does?). The rules Streambot follows:

- Cut at **paragraph boundaries**, so a passage reads as a unit.
- Aim for about **1,800 characters** (~450 tokens, `RAG_CHUNK_CHARS`).
- **Overlap** each chunk with the end of the previous one (200 characters), so an answer that straddles a boundary is fully inside at least one chunk.
- Remember the **page** and the nearest **heading**: they become the citation ("report.pdf, p. 12").
- A paragraph longer than the target is split at **sentence ends**, then by length as a last resort.

```python src="AI/app/rag/chunker.py" fn="chunk_pages"
```

It is a pure function (pages in, chunks out: no database, no network), which is why it is easy to test and to play with. Here it is, running on a tiny policy document with a deliberately small chunk size so you can see the pieces:

```python run include="AI/app/chat/history.py:approx_tokens|AI/app/rag/extract.py:Page|AI/app/rag/chunker.py:_HEADING,_PARAGRAPHS,_SENTENCE_END,Chunk,_split_long,_tail,chunk_pages" title="The real chunker on a small document" level=medium
sample = """# Refund policy

Refunds are issued within five business days of the return. The money goes back to the card you paid with.

## Exceptions

Digital goods cannot be refunded once downloaded. Gift cards are never refundable.

# Shipping

Shipping is free over fifty dollars. Orders to remote islands may take three extra weeks."""

for c in chunk_pages([Page(None, sample)], target_chars=230, overlap_chars=60):
    print(f"chunk {c.ordinal} | heading = {c.heading!r} | about {c.tokens} tokens")
    print("   " + c.content.replace("\n", "\n   "))
    print()

# Try it: change overlap_chars to 0 and watch the repeated lines disappear.
# Try target_chars=100 or 600 and see how the passages (and their headings) change.
```

Look at what happens with overlap: the end of one chunk reappears at the start of the next. And each chunk carries the heading in force where its *new* text begins, which is what the UI later shows as the source's title.

:::warn Chunking is the quiet quality lever
Most "the AI missed the answer" problems in RAG are chunking problems, not model problems: a table split across two chunks, a heading separated from its text, a paragraph so large that its meaning is blurred. If you tune one thing, tune this, and measure the effect (see the retrieval lesson).
:::

## Step 3: embed the chunks

```python src="AI/app/rag/embeddings.py" fn="OpenAIEmbedder"
```

This is the second place (after the chat model client) that talks to OpenAI, and it follows the same rules:

- The key lives only on the server.
- **Retries are bounded** and only for transient errors (the same classification as the chat client); a bad key or quota error fails immediately with a safe message.
- **Batches** of 64 texts per request, because the API allows many but small batches fail small.
- Results are matched to inputs **by `index`**, not by position, and a wrong count or a vector of the wrong size is refused, so a provider change can never silently corrupt the index.
- **Usage is reported** (`input_tokens`, as the provider says; `None` if it says nothing) so the cost can be recorded.

Embeddings cost money, but very little. The model has no output tokens, only input:

```python run title="What does it cost to index a book?" level=easy
price_per_million = 0.02          # USD per 1M tokens (text-embedding-3-small; check OpenAI's pricing page)
pages = 300
tokens_per_page = 500             # a dense page is roughly 400-600 tokens

tokens = pages * tokens_per_page
cost = tokens * price_per_million / 1_000_000
print(f"{pages} pages = about {tokens:,} tokens -> ${cost:.4f} to index (once)")
print(f"embedding a question of 20 tokens costs ${20 * price_per_million / 1_000_000:.8f}")
```

Indexing a 300-page document costs a fraction of a cent, once. The price lives in `AI/data/prices.toml` next to the chat models (not in code), and every embedding call is recorded as its own **purpose** (`embedding`) on the Usage page, alongside replies, titles and summaries.

```toml src="AI/data/prices.toml" from='[models."text-embedding-3-small"]' to="output"
```

## Step 4: store them with pgvector

Vectors go into PostgreSQL, next to everything else, using the **pgvector** extension:

```sql src="AI/migrations/003_documents.sql" from="CREATE TABLE chunks (" to="CREATE INDEX idx_chunks_scope"
```

Four things to notice:

- **`embedding vector(1536)`**: a column type for exactly that many numbers. It is fixed by the model, which is why `OPENAI_EMBEDDING_MODEL` must produce 1,536 dimensions, and why the code refuses vectors of any other size.
- **An HNSW index** (`vector_cosine_ops`). Comparing a query to every stored vector is exact but slow at scale. HNSW (Hierarchical Navigable Small World) is a graph that finds *approximately* the nearest neighbours in milliseconds: it trades a little recall for a lot of speed.
- **A full-text column** (`tsv`) maintained by Postgres itself, for keyword search. It is the second half of *hybrid* search in the next lesson.
- **`user_id` and `conversation_id` are copied onto every chunk on purpose.** Every search filters on them directly, so one user's question can never touch another user's chunks even through a mistake in a join.

Why a vector extension inside Postgres rather than a separate vector database? One system to back up and secure, **one transaction** around "mark the document ready and insert all its chunks", and the retrieval query can filter by user in the same statement. A dedicated vector database earns its place at much larger scale; for this app it would be extra moving parts.

```python src="AI/app/db.py" fn="Database.complete_document"
```

That transaction is what keeps the state honest: a document is **never** `ready` with only some of its chunks. If anything fails (a vector of the wrong size, say), everything rolls back and the document stays `processing` (a test proves it).

:::keypoints
- **RAG** = retrieve the relevant passages, then let the model answer from only those. It beats pasting a whole file: cheaper, and it fits.
- An **embedding** is a vector of numbers representing meaning; **cosine similarity** measures how close two meanings are.
- **Chunk** at paragraph boundaries, ~450 tokens, with **overlap**, and keep **page** and **heading** for citations.
- Embedding calls are **bounded-retry**, **batched**, **validated** (count and size) and **costed** (purpose `embedding`).
- **pgvector** stores `vector(1536)`; an **HNSW** index finds approximate nearest neighbours fast; user and chat ids are copied onto each chunk.
- Store a document's chunks and its `ready` status in **one transaction**.
:::

:::exercise Choose the chunk size
You index a legal contract where one clause is often 3,000 characters long, and questions are usually about a single clause. What happens with 1,800-character chunks, and what would you change?
:::

:::solution Show the answer
A clause longer than the chunk size is split at sentence ends, so one clause becomes two chunks, and a question may match only half of it (the other half is only reachable through the 200-character overlap). Options: raise `RAG_CHUNK_CHARS` to about 3,500 so a clause usually stays whole (at the cost of blurrier, larger passages and more tokens per search), or add structure-aware splitting that cuts at clause numbers. Whichever you pick, test it against real questions rather than guessing.
:::

```quiz
? Why not embed a whole 200-page PDF as one vector?
- Because the API rejects long input.
+ One vector is one summary of everything, so no question could find a specific passage.
- Because vectors must be 1,536 characters long.
> Specific passages make specific vectors; that is what retrieval needs.

? Why do neighbouring chunks overlap?
- To make the index bigger.
+ So an answer that straddles a boundary is still fully inside at least one chunk.
- Because the embedding model requires it.
> Overlap trades a little duplication for not losing boundary-spanning answers.

? What does an HNSW index give up in exchange for speed?
- Nothing; it is exact.
+ A little recall: it finds approximately the nearest vectors, not guaranteed the exact ones.
- The ability to filter by user.
> Approximate nearest-neighbour search is what makes vector search fast at scale.

? Why are user_id and conversation_id copied onto every chunk?
- For decoration.
+ So every search can filter on them directly, and one user's query cannot reach another's chunks even via a bug in a join.
- Because Postgres requires it for vector columns.
> Defence in depth: the isolation rule is in the same table the search reads.
```
