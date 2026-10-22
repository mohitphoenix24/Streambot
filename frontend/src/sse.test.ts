import { describe, expect, it } from "vitest";
import { SSEParser } from "./sse";

const wire = (event: string, data: object) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;

describe("SSEParser", () => {
  it("parses one complete event", () => {
    const events = new SSEParser().push(wire("delta", { text: "Hello" }));
    expect(events).toEqual([{ event: "delta", data: '{"text":"Hello"}' }]);
  });

  it("parses several events delivered in a single chunk", () => {
    const events = new SSEParser().push(wire("delta", { text: "a" }) + wire("delta", { text: "b" }));
    expect(events.map((e) => JSON.parse(e.data).text)).toEqual(["a", "b"]);
  });

  it("does NOT assume one network chunk is one event (event split mid-JSON)", () => {
    const parser = new SSEParser();
    // chunk 1 ends in the middle of the JSON string
    expect(parser.push('event: delta\ndata: {"text":"Hel')).toEqual([]);
    expect(parser.pending).toContain("Hel");
    // chunk 2 completes it
    const events = parser.push('lo"}\n\n');
    expect(events).toHaveLength(1);
    expect(JSON.parse(events[0].data)).toEqual({ text: "Hello" });
    expect(parser.pending).toBe("");
  });

  it("handles the terminator itself being split across chunks", () => {
    const parser = new SSEParser();
    expect(parser.push('event: done\ndata: {"ok":true}\n')).toEqual([]); // only ONE newline so far
    expect(parser.push("\n")).toHaveLength(1); // the second newline finishes the event
  });

  it("survives being fed one character at a time", () => {
    const parser = new SSEParser();
    const stream = wire("delta", { text: "x" }) + wire("done", { n: 1 });
    const events = [...stream].flatMap((ch) => parser.push(ch));
    expect(events.map((e) => e.event)).toEqual(["delta", "done"]);
  });

  it("keeps a trailing partial event buffered alongside complete ones", () => {
    const parser = new SSEParser();
    const events = parser.push(wire("delta", { text: "a" }) + "event: delta\ndata: {");
    expect(events).toHaveLength(1);
    expect(parser.pending).toBe("event: delta\ndata: {");
  });

  it("accepts CRLF line endings, even split between chunks", () => {
    const parser = new SSEParser();
    expect(parser.push("event: x\r\ndata: 1\r")).toEqual([]);
    expect(parser.push("\n\r\n")).toEqual([{ event: "x", data: "1" }]);
  });

  it("ignores comment lines and defaults the event name to 'message'", () => {
    expect(new SSEParser().push(": keep-alive\n\ndata: hi\n\n")).toEqual([{ event: "message", data: "hi" }]);
  });
});
