// Incremental Server-Sent-Events parser.
//
// THE KEY IDEA: network chunks are not events.
//
// TCP/HTTP delivers a stream of BYTES. It guarantees order, not boundaries. The
// server wrote "event: delta\ndata: {...}\n\n" as one unit, but the network may
// hand it to us in any slicing: several events in one chunk, or one event cut
// in half. For example, two consecutive chunks might be:
//
//     chunk 1:  event: delta\ndata: {"text":"Hel
//     chunk 2:  lo"}\n\n
//
// If we JSON.parse'd chunk 1 on its own it would throw. So we keep an internal
// BUFFER of text received so far and only emit an event once we have seen its
// terminator (a blank line, i.e. "\n\n"). Whatever follows the last "\n\n" is an
// incomplete event and stays in the buffer until more bytes arrive.
//
//     network chunk -> decode -> append to buffer -> split on "\n\n"
//                   -> complete events out, remainder stays buffered

export interface SSEEvent {
  event: string;
  data: string;
}

export class SSEParser {
  private buffer = "";

  /** Feed one decoded chunk. Returns every event that became complete. */
  push(chunk: string): SSEEvent[] {
    // Normalise CRLF so "\r\n\r\n" terminators work too. We re-normalise the whole
    // buffer (not just the chunk) because a "\r" and its "\n" can arrive in
    // different chunks.
    this.buffer = (this.buffer + chunk).replace(/\r\n/g, "\n");

    const events: SSEEvent[] = [];
    let end: number;
    while ((end = this.buffer.indexOf("\n\n")) !== -1) {
      const block = this.buffer.slice(0, end);
      this.buffer = this.buffer.slice(end + 2);
      const parsed = parseBlock(block);
      if (parsed) events.push(parsed);
    }
    return events;
  }

  /** Text received but not yet terminated by a blank line. Mostly for tests/debugging. */
  get pending(): string {
    return this.buffer;
  }
}

function parseBlock(block: string): SSEEvent | null {
  let event = "message"; // SSE default when no `event:` line is present
  const data: string[] = [];

  for (const line of block.split("\n")) {
    if (line === "" || line.startsWith(":")) continue; // blank or comment (keep-alive)
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1); // one optional space after the colon

    if (field === "event") event = value;
    else if (field === "data") data.push(value); // multiple data: lines are joined with \n
  }

  if (data.length === 0) return null; // an event with no data is not dispatched
  return { event, data: data.join("\n") };
}
