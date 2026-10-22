import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, createConversation, getConversation, streamMessage } from "./api";
import { formatMoney } from "./format";
import { describeRate, useRateInfo, useUsdToInr, useWebSearchAvailable } from "./rate";
import { ArrowDownIcon, GlobeIcon, LogoMark, SendIcon, StopIcon } from "./Icons";
import Message, { type ViewMessage } from "./Message";
import type { Conversation } from "./types";

interface Props {
  /** null = a fresh draft chat: nothing exists on the server until the first message is sent. */
  conversationId: string | null;
  /** Called once, when a draft chat is saved because its first message is being sent. */
  onCreated: (conversation: Conversation) => void;
  onTitle: (conversationId: string, title: string) => void;
  /** Called after any message exchange so the sidebar can re-sort by activity. */
  onActivity: () => void;
}

type LoadState = "loading" | "ready" | "error";

const isAbort = (err: unknown) => err instanceof DOMException && err.name === "AbortError";

const SUGGESTIONS = [
  "Explain how server-sent events work, with a small example",
  "Review this function for bugs and suggest improvements",
  "Draft a concise project update from my rough notes",
  "Compare two approaches and recommend one",
];

let tempCounter = 0;
const tempMessage = (role: "user" | "assistant", content: string): ViewMessage => ({
  id: `tmp-${role}-${++tempCounter}`,
  role,
  content,
  status: role === "user" ? "complete" : "streaming",
  input_tokens: null,
  cached_input_tokens: null,
  output_tokens: null,
  reasoning_tokens: null,
  total_tokens: null,
  cost_usd: null,
  stop_reason: null,
  model: null,
  served_model: null,
  ttft_ms: null,
  latency_ms: null,
  sources: [],
  web_searches: null,
  cache_savings_usd: null,
  created_at: new Date().toISOString(),
});

// Remembered per browser; failing to read/write it must never break the chat.
const SEARCH_KEY = "web-search";
const readSearchPref = () => {
  try {
    return localStorage.getItem(SEARCH_KEY) !== "off";
  } catch {
    return true;
  }
};

export default function Chat({ conversationId, onCreated, onTitle, onActivity }: Props) {
  const rate = useUsdToInr();
  const rateInfo = useRateInfo();
  const searchAvailable = useWebSearchAvailable();
  const [webSearch, setWebSearch] = useState(readSearchPref);
  const [messages, setMessages] = useState<ViewMessage[]>([]);
  const [title, setTitle] = useState("");
  const [totals, setTotals] = useState({ cost: 0, tokens: 0, input: 0, output: 0, unpriced: 0 });
  const [load, setLoad] = useState<LoadState>(conversationId ? "loading" : "ready");
  const [streaming, setStreaming] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);
  const [historyTruncated, setHistoryTruncated] = useState(false);
  const [historySummarized, setHistorySummarized] = useState(false);
  const [input, setInput] = useState("");
  const [showJump, setShowJump] = useState(false); // "jump to latest" while scrolled up

  // The AbortController of the request in flight (null when idle). Calling
  // .abort() makes the browser close the connection; see Stop below.
  const abortRef = useRef<AbortController | null>(null);
  // The id in use. Starts as the prop; a draft chat gets one when its first message is sent.
  // A ref (not state) so saving a draft never re-runs the load effect, which would abort the stream.
  const idRef = useRef(conversationId);
  const mountedRef = useRef(true);
  const bottomRef = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Load the conversation from the server. The DATABASE is the source of
  // truth, which is why a page refresh brings everything back.
  const reload = useCallback(async () => {
    const id = idRef.current;
    if (!id) return;
    try {
      const detail = await getConversation(id);
      if (!mountedRef.current) return;
      setMessages(detail.messages);
      setTitle(detail.title);
      setTotals({
        cost: detail.total_cost_usd,
        tokens: detail.total_tokens,
        input: detail.total_input_tokens,
        output: detail.total_output_tokens,
        unpriced: detail.unpriced_messages,
      });
      setLoad("ready");
    } catch {
      if (mountedRef.current) setLoad("error");
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    if (idRef.current) {
      setLoad("loading");
      void reload();
    }
    return () => {
      mountedRef.current = false;
      // Navigating away mid-reply cancels it (the server saves the partial text),
      // rather than burning tokens for a screen nobody is looking at.
      abortRef.current?.abort();
    };
  }, [reload]);

  useEffect(() => {
    if (stickToBottom.current) bottomRef.current?.scrollIntoView({ block: "end" });
  }, [messages]);

  // Grow the composer with its content (up to the CSS max-height), shrink it back after sending.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [input]);

  // Ready to type as soon as the conversation has loaded.
  useEffect(() => {
    if (load === "ready") textareaRef.current?.focus();
  }, [load]);

  function onScroll(e: React.UIEvent<HTMLDivElement>) {
    const el = e.currentTarget;
    // Only auto-follow the stream while the user is at the bottom; if they
    // scrolled up to read, don't yank them back down on every token.
    stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    setShowJump(el.scrollHeight - el.scrollTop - el.clientHeight > 240);
  }

  function jumpToLatest() {
    stickToBottom.current = true;
    bottomRef.current?.scrollIntoView({ block: "end", behavior: "smooth" });
  }

  async function send() {
    const text = input.trim();
    if (!text || streaming) return;

    setBanner(null);
    if (!idRef.current) {
      // First message of a draft chat: save the conversation now, so abandoned
      // "New chat" tabs never leave empty conversations behind.
      try {
        const conv = await createConversation();
        idRef.current = conv.id;
        onCreated(conv);
      } catch (err) {
        setBanner(err instanceof ApiError ? err.message : "Could not reach the server. Is the backend running?");
        return;
      }
    }
    const convId = idRef.current as string; // set above for a draft
    setHistoryTruncated(false);
    setHistorySummarized(false);
    stickToBottom.current = true;

    // Optimistic UI: show the user's message and an empty assistant bubble
    // ("Thinking...") immediately, before the server has answered anything.
    const tmpUser = tempMessage("user", text);
    const tmpAssistant = tempMessage("assistant", "");
    let assistantId = tmpAssistant.id;
    setMessages((m) => [...m, tmpUser, tmpAssistant]);
    setInput("");
    setStreaming(true);

    const controller = new AbortController();
    abortRef.current = controller;

    // `id` is read NOW (when the event arrives), not when React later runs the
    // updater: updaters are queued, so they must not depend on a mutable variable.
    const patchAssistant = (patch: (m: ViewMessage) => Partial<ViewMessage>) => {
      const id = assistantId;
      setMessages((ms) => ms.map((m) => (m.id === id ? { ...m, ...patch(m) } : m)));
    };

    try {
      await streamMessage(
        convId,
        text,
        {
          // `start`: swap our temporary ids for the real database ids.
          onStart: (e) => {
            // Capture the OLD id first. React runs this updater later, after we have
            // reassigned `assistantId` below; reading the variable inside the updater
            // would compare against the NEW id and never rename the placeholder
            // (every later delta would then match no message and nothing would show).
            const placeholderId = assistantId;
            setMessages((ms) =>
              ms.map((m) =>
                m.id === tmpUser.id
                  ? { ...m, id: e.user_message_id }
                  : m.id === placeholderId
                    ? { ...m, id: e.assistant_message_id }
                    : m,
              ),
            );
            assistantId = e.assistant_message_id;
            setHistoryTruncated(e.history_truncated);
            setHistorySummarized(e.history_summarized);
          },
          // `delta`: append the new text immediately. THIS is what makes the
          // answer appear to be typed out live.
          onDelta: (e) => patchAssistant((m) => ({ content: m.content + e.text })),
          // The model decided to search: show it live ("Searching the web for ...").
          onToolCall: (e) =>
            patchAssistant((m) => ({
              toolSteps: [...(m.toolSteps ?? []), { id: e.id, query: e.query, status: "running" as const }],
            })),
          // The search came back: remember its sources (the answer will cite them as [n]).
          onToolResult: (e) =>
            patchAssistant((m) => ({
              sources: [...m.sources, ...e.sources],
              toolSteps: (m.toolSteps ?? []).map((t) =>
                t.id === e.id
                  ? { ...t, status: e.error ? ("error" as const) : ("done" as const), count: e.sources.length, error: e.error }
                  : t,
              ),
            })),
          // `done`: the reply is final. Stop loading, record tokens and cost.
          onDone: (e) => {
            patchAssistant(() => ({
              status: "complete",
              stop_reason: e.stop_reason,
              // Straight from the provider's final usage; null stays null ("unavailable").
              input_tokens: e.usage?.input_tokens ?? null,
              cached_input_tokens: e.usage?.cached_input_tokens ?? null,
              output_tokens: e.usage?.output_tokens ?? null,
              reasoning_tokens: e.usage?.reasoning_tokens ?? null,
              total_tokens: e.usage?.total_tokens ?? null,
              cost_usd: e.cost?.total_cost ?? null,
              model: e.requested_model,
              served_model: e.served_model,
              ttft_ms: e.ttft_ms,
              latency_ms: e.latency_ms,
              web_searches: e.web_searches || null,
              cache_savings_usd: e.cost?.cache_savings ?? null,
            }));
            setTotals({
              cost: e.conversation_total_cost_usd,
              tokens: e.conversation_total_tokens,
              input: e.conversation_total_input_tokens,
              output: e.conversation_total_output_tokens,
              unpriced: e.unpriced_messages,
            });
          },
          // `error`: keep whatever text we already have and mark the bubble.
          onError: (e) => patchAssistant(() => ({ status: "error", errorMessage: e.message })),
          onTitle: (e) => {
            setTitle(e.title);
            onTitle(convId, e.title);
          },
        },
        controller.signal,
        webSearch && searchAvailable,
      );
    } catch (err) {
      const started = assistantId !== tmpAssistant.id; // did the server acknowledge us?
      if (isAbort(err)) {
        // User pressed Stop (or navigated away). Keep the partial text.
        patchAssistant(() => ({ status: "cancelled" }));
      } else if (!started) {
        // Rejected before any streaming (413 too long, 429 rate limit, server
        // down...). Nothing was saved server-side: roll the optimistic UI back
        // and give the user their text back.
        setMessages((ms) => ms.filter((m) => m.id !== tmpUser.id && m.id !== tmpAssistant.id));
        setInput(text);
        setBanner(err instanceof ApiError ? err.message : "Could not reach the server. Is the backend running?");
      } else {
        // Connection died mid-stream with no `error` event from the server.
        patchAssistant(() => ({ status: "error", errorMessage: "The connection was lost." }));
      }
      // Re-sync with the database shortly (it records how the reply really ended).
      if (!isAbort(err) || started) window.setTimeout(() => !abortRef.current && void reload(), 800);
    } finally {
      abortRef.current = null;
      setStreaming(false);
      onActivity();
      textareaRef.current?.focus();
    }
  }

  // STOP: abort() rejects the pending fetch AND closes the underlying connection.
  // The server sees the client vanish, stops reading from OpenAI, and saves the
  // partial answer with status='cancelled'. Hiding the UI alone would NOT stop
  // the model: it would keep generating (and billing) tokens nobody will read.
  const stop = () => abortRef.current?.abort();

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Escape" && streaming) {
      stop();
      return;
    }
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send();
    }
  }

  const costLabel =
    totals.unpriced > 0 && totals.cost === 0
      ? "cost n/a (no price configured)"
      : totals.unpriced > 0
        ? `${formatMoney(totals.cost, rate)} + ${totals.unpriced} unpriced`
        : formatMoney(totals.cost, rate);

  return (
    <section className="chat">
      <header className="chat-header">
        <h2>{title || "New chat"}</h2>
        {totals.tokens > 0 && (
          <span
            className="totals"
            title="Sums of the usage OpenAI reported for every reply in this conversation (replies without reported usage are not counted)"
          >
            <span className="chip">
              <b>{totals.input.toLocaleString()}</b> input tokens
            </span>
            <span className="chip">
              <b>{totals.output.toLocaleString()}</b> output tokens
            </span>
            <span className="chip chip-cost" title={describeRate(rateInfo)}>
              {costLabel}
            </span>
          </span>
        )}
      </header>

      <div className="messages" onScroll={onScroll}>
        <div className="thread">
          {load === "loading" && (
            <div className="skeleton-thread" aria-label="Loading conversation">
              <span /> <span /> <span />
            </div>
          )}
          {load === "error" && (
            <div className="state state-error">
              Could not load this conversation.{" "}
              <button className="link" onClick={() => void reload()}>
                Retry
              </button>
            </div>
          )}
          {load === "ready" && messages.length === 0 && (
            <div className="empty">
              <LogoMark width={48} height={48} />
              <h3>How can I help today?</h3>
              <p>Ask anything. Replies stream in as they are generated.</p>
              <div className="suggestions">
                {SUGGESTIONS.map((q) => (
                  <button
                    key={q}
                    type="button"
                    onClick={() => {
                      setInput(q);
                      textareaRef.current?.focus();
                    }}
                  >
                    {q}
                  </button>
                ))}
              </div>
            </div>
          )}
          {messages.map((m) => (
            <Message key={m.id} message={m} />
          ))}
          {historySummarized && !historyTruncated && (
            <div className="notice notice-info">
              This conversation is long: earlier messages are summarized so the model keeps the context.
            </div>
          )}
          {historyTruncated && (
            <div className="notice notice-info">
              This conversation is long: older messages were left out of what the model sees (history budget).
            </div>
          )}
          <div ref={bottomRef} />
        </div>
      </div>

      {showJump && (
        <button type="button" className="jump" onClick={jumpToLatest}>
          <ArrowDownIcon /> Latest
        </button>
      )}

      {banner && (
        <div className="banner" role="alert">
          {banner}
          <button className="link" onClick={() => setBanner(null)}>
            Dismiss
          </button>
        </div>
      )}

      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault();
          void send();
        }}
      >
        <div className="composer-box">
          <textarea
            ref={textareaRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Message…"
            rows={1}
            disabled={load !== "ready"}
            aria-label="Message"
          />
          {searchAvailable && (
            <button
              type="button"
              className={`search-toggle ${webSearch ? "on" : ""}`}
              aria-pressed={webSearch}
              title={webSearch ? "Web search is on: the model can look things up and cite sources" : "Web search is off"}
              onClick={() =>
                setWebSearch((on) => {
                  try {
                    localStorage.setItem(SEARCH_KEY, on ? "off" : "on");
                  } catch {
                    /* preference just won't persist */
                  }
                  return !on;
                })
              }
            >
              <GlobeIcon /> <span>Search</span>
            </button>
          )}
          {streaming ? (
            <button type="button" className="send send-stop" onClick={stop} title="Stop (Esc)" aria-label="Stop generating">
              <StopIcon />
            </button>
          ) : (
            <button type="submit" className="send" disabled={!input.trim() || load !== "ready"} title="Send (Enter)" aria-label="Send message">
              <SendIcon />
            </button>
          )}
        </div>
        <p className="hint">
          <kbd>Enter</kbd> to send · <kbd>Shift</kbd> + <kbd>Enter</kbd> for a new line
          {streaming && (
            <>
              {" "}
              · <kbd>Esc</kbd> to stop
            </>
          )}
        </p>
      </form>
    </section>
  );
}
