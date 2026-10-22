---
title: The upload pipeline: safe, background, honest
level: hard
time: 55 min
summary: Treating uploaded files as hostile input, reading PDF/Word/text safely, indexing in the background with a status machine, keeping the index consistent when things fail, and testing all of it without OpenAI.
tags: file upload, validation, magic bytes, zip bomb, background tasks, status machine, atomicity, testing, test doubles
---

A chat message is a short string you can cap at 8,000 characters. A **file** is an arbitrary blob from the internet that is about to be fed to a parser, read into memory, split, sent to a paid API and stored. Every step is a place for it to go wrong, and for someone to go wrong *on purpose*. This lesson is about handling that with care, and about telling the user the truth when something fails.

:::flow Life of an attachment
Upload | `POST /conversations/{id}/documents`: size caps, rate limit, ownership check
Validate | extension allowlist, then the **content** must match the extension
Register | a `documents` row with status `processing`; the endpoint answers `202` at once
Index | a background task extracts, chunks, embeds and stores (one transaction)
Ready | status becomes `ready` (or `failed` with a safe reason); the browser polls
Use | the model can now search it; deleting it removes its chunks
:::

## Step 1: never trust the upload

Before any parser sees a byte, the request is checked in layers. Each layer stops a different problem:

| Layer | Stops |
|---|---|
| Per-route body cap, from `Content-Length` (`BodySizeLimitMiddleware`) | a huge upload being read into memory at all |
| `file.read(limit + 1)` | a client that lies about (or omits) the length: never read more than one byte past the cap |
| Per-user upload rate limit (`UPLOAD_RATE_LIMIT_PER_MINUTE`) | one account flooding the server with indexing work |
| `MAX_DOCUMENTS_PER_CHAT` | unbounded storage and embedding cost per chat |
| Ownership check (`require_conversation`) | uploading into someone else's chat (`404`) |
| Filename sanitising | `../../etc/passwd`, Windows paths and control characters |
| `detect_kind` | wrong or disguised file types (next section) |

```python src="AI/app/main.py" from='@app.post("/conversations/{conversation_id}/documents"' to="return document"
```

Notice the order: cheap checks first, expensive ones last, and **nothing is stored until the file has passed validation**. A rejected file leaves no row behind.

```python src="AI/app/main.py" from="BodySizeLimitMiddleware," to="overrides=["
```

The ordinary body cap is about 100 KB (a chat message never needs more). Only the upload route gets the large cap, via a regular expression on the path. That is the "least privilege" idea applied to request sizes.

## Step 2: the extension is not the truth

A program renamed `invoice.pdf` still *says* it is a PDF. So the app checks that the **content** agrees with the name:

```python src="AI/app/rag/extract.py" fn="detect_kind"
```

- A PDF must begin with `%PDF-`; a Word file must be a real ZIP containing `word/document.xml`; text and Markdown must not contain NUL bytes (a sign of binary data).
- **Zip-bomb guard.** A `.docx` is a ZIP archive, and a tiny archive can expand to gigabytes. The total *declared uncompressed size* is checked against a limit before anything is opened.
- Old `.doc` gets a friendly message rather than a confusing failure.

Run the real function against some hostile and honest inputs:

```python run include="AI/app/rag/extract.py:ALLOWED_EXTENSIONS,MAX_DOCX_UNCOMPRESSED,ExtractionError,detect_kind" title="What does detect_kind accept?" level=medium
import io, zipfile
from pathlib import PurePosixPath

def make_docx_like(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buffer.getvalue()

samples = {
    "a real-looking PDF":        ("report.pdf", b"%PDF-1.4 ..."),
    "a program renamed to .pdf": ("invoice.pdf", b"MZ\x90\x00 this is an executable"),
    "a text file":               ("notes.txt", b"hello"),
    "binary data named .txt":    ("data.txt", b"ab\x00\x01\x02cd"),
    "an empty file":             ("empty.md", b""),
    "an old Word file":          ("old.doc", b"\xd0\xcf\x11\xe0"),
    "an image":                  ("photo.png", b"\x89PNG"),
    "a zip renamed to .docx":    ("fake.docx", make_docx_like({"hello.txt": "not a word document"})),
    "a real .docx structure":    ("memo.docx", make_docx_like({"word/document.xml": "<w:document/>"})),
}
for label, (name, data) in samples.items():
    try:
        print(f"ACCEPT  {label:28} -> {detect_kind(name, data)}")
    except ExtractionError as e:
        print(f"REFUSE  {label:28} -> {e.public_message}")

# A "zip bomb": tiny on disk, enormous when expanded. Lower the limit to see the guard fire.
MAX_DOCX_UNCOMPRESSED = 10_000
bomb = make_docx_like({"word/document.xml": "A" * 1_000_000})
print(f"\nbomb is {len(bomb):,} bytes compressed")
try:
    detect_kind("bomb.docx", bomb)
except ExtractionError as e:
    print("REFUSE  zip bomb                     ->", e.public_message)
```

## Step 3: read the text out

```python src="AI/app/rag/extract.py" fn="extract"
```

Parser details that came from real documents rather than theory:

- **PDF**: text is read **page by page**, so the page number can become a citation. A page that fails to parse becomes empty instead of losing the whole file. A password-protected PDF gets a clear message. A PDF with *no extractable text* is a scan (an image): the user is told it needs OCR, which Streambot does not include.
- **Word**: the body is walked **in order**, so a table stays where it is, each table row becomes one readable line, and a heading becomes a Markdown `#` line, which lets the chunker treat Word and Markdown identically.
- **Text**: UTF-8 first (with or without a byte-order mark), falling back to the old Windows encoding.
- **Any parser crash** becomes "This file could not be read. It may be damaged." The raw error goes to the server log, never to the browser.
- Limits on **pages**, **characters** and later **chunks** keep one file from exhausting memory or running up an embedding bill.

Parsing a big PDF is CPU work, so it runs in a **worker thread** (`run_in_threadpool`): the event loop that streams everyone else's replies keeps running.

## Step 4: index in the background

Embedding a long PDF takes seconds, and the browser should not stare at a spinner for that long. So the upload endpoint answers `202 Accepted` immediately with the document in state `processing`, and a background task finishes the job:

```python src="AI/app/rag/ingest.py" fn="DocumentManager"
```

The status is a small state machine, and **every path ends in a defined state**:

| Outcome | What happens |
|---|---|
| Success | `ready`, with page and chunk counts |
| Unreadable, empty, too large | `failed` with a short, safe explanation (an `ExtractionError`'s public message) |
| OpenAI outage or bad key | `failed` with the translated, safe provider message |
| Anything unexpected | logged with the stack trace on the server; the user sees only "This file could not be processed." |
| The user deleted the document while it was indexing | the final insert hits a foreign-key error; that is recognised and ignored, because there is nothing left to update |
| The server was killed mid-indexing | at the next startup, every `processing` row is set to `failed` ("Processing was interrupted. Please upload it again.") |

Two details deserve a second look:

- **`self._tasks` keeps a strong reference** to every running task. Python's event loop holds only a *weak* reference to tasks, so a task nobody holds can be garbage-collected mid-run. The `add_done_callback` removes it when it ends.
- **An unhandled exception in a background task is invisible**: nobody awaits it, so without the catch-all the document would stay `processing` forever. "No silent failure" is a rule here too.

The browser polls the document list every 1.5 seconds while anything is `processing`, and stops by itself when everything is settled (see the client lesson).

## Step 5: keep the index consistent

The last write is the one that matters, and it is one transaction:

```python src="AI/app/db.py" fn="Database.complete_document"
```

Old chunks are deleted, the new ones inserted and the status flipped to `ready` **together**. If the chunk insert fails halfway (a vector of the wrong size, say), the transaction rolls back: the document is *not* `ready`, and no orphan chunks remain. A test inserts one good and one bad chunk and asserts exactly that.

And cleanup follows the same principle as the rest of the schema: chunks have `ON DELETE CASCADE` to their document, documents to their conversation, conversations to their user. Delete a chat and its files, passages and vectors go with it, with no cleanup code to forget.

## The cost, in the open

Every embedding call (indexing a file, embedding a question) is recorded in `llm_calls` with purpose `embedding`, with its usage and cost (or `NULL` cost if no price is configured; never a guess). The Usage page shows **document indexing** as its own line next to replies, titles and summaries. Searching your own files is deliberately **not** counted as a "web search".

## Testing it without OpenAI

None of the 440+ backend tests calls OpenAI. Three test doubles make that possible:

```python src="AI/tests/helpers.py" fn="FakeEmbedder"
```

- **`FakeEmbedder`** returns deterministic vectors (hashed words) and can be told to fail, so indexing, search, cost recording and every failure path are testable.
- **`make_pdf` / `make_docx`** build *real* files in memory, so the real parsers (pypdf, python-docx) are exercised, not mocks of them.
- **A real PostgreSQL** per test (cloned from a migrated template), so pgvector, the HNSW and GIN indexes, cascades and the fusion query run on the real engine.

The suite covers the happy path for each format, every refusal (wrong type, disguised files, zip bombs, encrypted and empty PDFs), background outcomes (failure, deleted mid-index, crash recovery), the OpenAI embedder against a stub client (batching, retries, bad responses), and the HTTP layer including **another user trying to see, upload to or delete from your chat**.

:::keypoints
- An upload is **hostile input**: cap the size in layers, check ownership, sanitise the filename, and validate the **content**, not just the extension.
- A `.docx` is a ZIP: guard against **zip bombs**. Put limits on pages, characters and chunks.
- Parser failures become **short, safe messages**; the detail goes to the server log.
- Index in the **background** and answer `202`; give the document a **status** and let the browser poll.
- **Every path ends in a defined state**: ready, failed with a reason, a quiet no-op when deleted, or `failed` after a crash.
- Hold **strong references** to background tasks, and never let an exception vanish silently.
- Write a document's chunks and its `ready` flag in **one transaction**; use **cascades** so cleanup cannot be forgotten.
- Test with **real files and a real database**, and a **fake embedder** instead of the network.
:::

:::exercise A very large upload
A user uploads a 9 MB PDF with 280 pages while the server is also streaming replies to 30 other people. What could go wrong, and which of Streambot's choices keeps it under control?
:::

:::solution Show the answer
Parsing and embedding are heavy. Risks: blocking the event loop (other users' streams stall), memory spikes, a long embedding bill, and the user thinking nothing is happening. Mitigations in the app: the size cap and page limit stop truly huge files; parsing runs in a **worker thread** so the loop keeps serving streams; the endpoint answers `202` immediately and the work is a **background task** with a visible `processing` status; embedding is **batched** and costed; a per-user **upload rate limit** and a per-chat document limit bound the total. What it does *not* do (a production upgrade) is run indexing in a separate worker process or queue, so a restart would interrupt it (it is then marked `failed`, and the user can upload again).
:::

```quiz
? Why is checking the file extension not enough?
- Extensions are case sensitive.
+ A file can be renamed: invoice.pdf may be a program. The content must match the claimed type.
- Browsers strip extensions.
> That is why a PDF must start with %PDF- and a .docx must be a real zip with word/document.xml.

? Why does the upload endpoint return 202 and not wait?
- To save bandwidth.
+ Indexing takes seconds; the file becomes ready in the background and the browser polls its status.
- Because 200 is reserved for GET.
> 202 Accepted means "received, still working on it".

? A background indexing task crashes with an unexpected error. What does the user see?
- Nothing; the document stays "processing" forever.
+ The document becomes "failed" with a generic safe message; the details are only in the server log.
- The raw exception text.
> Silent failure and leaked internals are both wrong.

? Why must chunks and the 'ready' status be written in one transaction?
- Transactions are faster.
+ So a document is never "ready" with only some of its chunks; any failure rolls everything back.
- Because Postgres requires it for vector columns.
> The status must never claim more than what is actually stored.
```
