# Streambot Guide

An interactive tutorial on how Streambot works: the architecture, every generative-AI idea in it, and the real code, explained in plain English first. 17 lessons, with examples that run in your browser.

## Open it

Double-click `index.html`, or serve the folder:

```bash
python3 -m http.server 8777 --directory docs      # then open http://localhost:8777
```

Python examples (marked **▶ Run**) execute in your browser with Pyodide, which is downloaded from a CDN the first time, so you need an internet connection for those. The lessons themselves read fine offline.

## What is inside

| Part | Lessons |
|---|---|
| Start here | What Streambot is · The architecture and the life of one message |
| How an LLM chat works | Tokens, statelessness and the request · The model layer · Streaming with SSE, Stop and crash recovery · Persistence · History and the token budget · Rolling-summary memory |
| Agents: tools and citations | Tool calling and the agent loop · Web search and citations |
| Cost, caching and production | Usage, pricing and cost accounting · Prompt caching and the Usage page · Reliability, security and limits |
| The React client | Reading the stream, state, safe rendering |
| Quality and career | Testing without the network · Concept map and interview answers · Where this stands: résumé value and next steps |

## The code in the lessons is real

Lessons are Markdown files in `content/`. They **quote the repository's real code by name** when the guide is built, so a lesson cannot drift from the app:

````text
```python src="AI/app/chat/history.py" fn="build_history"
```
````

* `src` + `fn` extracts functions, classes, `Class.method` or `UPPER_CASE` constants (Python) and functions, consts, interfaces and types (TypeScript).
* `src` + `from="text" to="text"` extracts a range of lines by anchor text, for code nested inside a function.
* `include="path:name,name|path:name"` on a `python run` block puts the real functions in front of your demo, and runs them in the browser.

If a quoted name or file no longer exists, **the build fails**. Every `python run` example is also executed with the local interpreter during the build, so a broken demo fails the build too.

## Rebuild after editing

```bash
python3 docs/build.py              # bundle the lessons and verify every runnable example
python3 docs/build.py --no-verify  # bundle only
python3 docs/build.py --watch      # rebuild when a lesson or a quoted source file changes
```

The build writes `assets/js/content.js` (committed, so the guide opens without building).

## Layout

```text
index.html          the page shell and the guide's configuration
build.py            bundles content/ and quotes the real source
content/            the lessons (Markdown), one folder per part
assets/js/          content.js (generated) and runner-python.js (Pyodide)
shared/             the lesson engine and design system (CSS, JS, CodeMirror, KaTeX, marked)
```

The engine in `shared/` is the same one used by the other Techbook courses, copied here so this folder is self-contained.
