import { SSEParser } from "./sse";
import type {
  Conversation,
  ConversationDetail,
  DeltaEvent,
  DoneEvent,
  ErrorEvent,
  StartEvent,
  StreamHandlers,
  TitleEvent,
  ToolCallEvent,
  ToolResultEvent,
  UsageReport,
} from "./types";

// The browser only ever talks to OUR backend. There is no OpenAI key (or any
// secret) in this bundle: Vite inlines VITE_* variables into public JavaScript,
// so anything here is visible to every visitor.
const API_URL: string = import.meta.env.VITE_API_URL ?? "http://localhost:8501";

/** A request the server rejected BEFORE any streaming began (400/404/409/413/429...). */
export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

/** The connection ended without a terminal `done`/`error` event (server died, network cut). */
export class StreamInterrupted extends Error {}

async function readError(res: Response): Promise<ApiError> {
  let detail = `Request failed (${res.status})`;
  try {
    const body = await res.json();
    if (typeof body?.detail === "string") detail = body.detail;
  } catch {
    /* body was not JSON; keep the generic message */
  }
  return new ApiError(detail, res.status);
}

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, init);
  if (!res.ok) throw await readError(res);
  return res.json() as Promise<T>;
}

export const listConversations = () => json<Conversation[]>("/conversations");
export const createConversation = () => json<Conversation>("/conversations", { method: "POST" });
export const getConversation = (id: string) => json<ConversationDetail>(`/conversations/${id}`);
export async function deleteConversation(id: string): Promise<void> {
  const res = await fetch(`${API_URL}/conversations/${id}`, { method: "DELETE" });
  if (!res.ok) throw await readError(res);
}
export interface Info {
  model: string | null;
  /** INR per 1 USD, or null when INR is hidden. */
  usd_to_inr: number | null;
  usd_to_inr_source: "live" | "fallback" | "fixed" | "off";
  usd_to_inr_as_of: string | null;
  web_search_available: boolean;
}
export const getInfo = () => json<Info>("/info");
/** `utcOffsetMinutes` (east positive) makes the backend bucket days on the user's clock. */
export const getUsage = (days: number, utcOffsetMinutes: number) =>
  json<UsageReport>(`/usage?days=${days}&utc_offset_minutes=${utcOffsetMinutes}`);

/**
 * Send a message and consume the SSE reply stream.
 *
 * WHY fetch() AND NOT EventSource?
 *   The browser's built-in EventSource is designed around GET: you cannot give it
 *   a request body, a method, or custom headers. Our chat request is
 *   POST /conversations/{id}/messages with a JSON body {"text": "..."}. So we
 *   use fetch() - which returns a Response whose `body` is a ReadableStream we
 *   can read incrementally - and parse the SSE format ourselves (see sse.ts).
 *   (The trade-off: EventSource gives free auto-reconnect; here we want NO
 *   automatic reconnect, since re-POSTing would duplicate the user's message.)
 *
 * CANCELLATION: pass an AbortSignal. controller.abort() makes fetch reject and
 * closes the TCP connection; the server notices the disconnect and stops.
 */
export async function streamMessage(
  conversationId: string,
  text: string,
  handlers: StreamHandlers,
  signal: AbortSignal,
  webSearch = true,
): Promise<void> {
  const res = await fetch(`${API_URL}/conversations/${conversationId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, web_search: webSearch }),
    signal,
  });
  if (!res.ok) throw await readError(res);
  if (!res.body) throw new StreamInterrupted("Response has no body to stream.");

  // res.body yields raw BYTES (Uint8Array). One character can span several bytes
  // in UTF-8 (emoji, accents, CJK), and the network may cut between them.
  // TextDecoderStream keeps the partial bytes between chunks and only emits
  // whole characters. Decoding each chunk independently would corrupt text.
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  const parser = new SSEParser();
  let sawTerminalEvent = false;

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      // One network chunk can contain 0, 1, or many events. The parser buffers.
      for (const evt of parser.push(value)) {
        const payload = JSON.parse(evt.data);
        switch (evt.event) {
          case "start":
            handlers.onStart?.(payload as StartEvent);
            break;
          case "delta":
            handlers.onDelta?.(payload as DeltaEvent);
            break;
          case "tool_call":
            handlers.onToolCall?.(payload as ToolCallEvent);
            break;
          case "tool_result":
            handlers.onToolResult?.(payload as ToolResultEvent);
            break;
          case "title":
            handlers.onTitle?.(payload as TitleEvent);
            break;
          case "done":
            sawTerminalEvent = true;
            handlers.onDone?.(payload as DoneEvent);
            break;
          case "error":
            sawTerminalEvent = true;
            handlers.onError?.(payload as ErrorEvent);
            break;
          // Unknown event types are ignored: lets the server add events without
          // breaking older clients.
        }
      }
    }
  } finally {
    reader.releaseLock();
  }

  if (!sawTerminalEvent) {
    throw new StreamInterrupted("The connection was lost before the response finished.");
  }
}
