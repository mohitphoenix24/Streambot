"""Server-Sent Events (SSE) wire format.

SSE is a plain-text protocol layered on an ordinary HTTP response whose
Content-Type is `text/event-stream`. The server simply never "finishes" the
response; it keeps writing small text blocks, and each block is one EVENT.

One event on the wire looks like this:

    event: delta
    data: {"text": "Hello"}
    <blank line>

  * `event:` names the event type, so the client can react differently to
    `delta`, `done`, `error`, ... (if omitted, the type defaults to "message").
  * `data:`  carries the payload. We always send ONE LINE of JSON, so the client
    just JSON.parse's it.
  * A BLANK LINE (i.e. the text ends with TWO newlines: "\\n" ending the data
    line, then another "\\n") is what terminates the event. SSE is line based:
    the receiver reads line by line, accumulating fields, and the empty line
    means "event complete, dispatch it". Without the second newline the client
    would wait forever for the event to end.

Why this suits LLM streaming: traffic is one-way (server -> browser) after the
initial request, it is just HTTP (works through proxies, load balancers, auth
middleware, curl), and the format is trivially debuggable by eye.

SSE is NOT WebSockets. WebSockets are a separate full-duplex protocol (the
connection is upgraded away from HTTP); use them when the client also needs to
stream data back continuously (voice, collaborative editing, multiplayer). Here
the client sends one request and the server streams a reply, so SSE is simpler.
"""
from __future__ import annotations

import json
from typing import Any


def sse(event: str, data: dict[str, Any]) -> str:
    # json.dumps never emits a raw newline inside a string (it writes "\n" as the
    # two characters backslash-n), so `data:` is guaranteed to stay on ONE line
    # and cannot accidentally terminate the event early.
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
