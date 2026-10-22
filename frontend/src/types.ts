// Shapes of the JSON the FastAPI backend sends. They mirror AI/app/schemas.py.

export type MessageStatus = "streaming" | "complete" | "cancelled" | "error";

/** A web page the model searched up; the reply cites it as [n]. */
export interface Source {
  n: number;
  title: string;
  url: string;
  snippet: string;
}

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  status: MessageStatus;
  // Provider-REPORTED usage. null = unavailable (never estimated).
  input_tokens: number | null;
  /** Subset of input_tokens that came from OpenAI's prompt cache (cheaper rate). */
  cached_input_tokens: number | null;
  output_tokens: number | null;
  /** Subset of output_tokens spent "thinking". Already inside output_tokens. */
  reasoning_tokens: number | null;
  total_tokens: number | null;
  /** null = unknown (no price configured for the model). NOT the same as free. */
  cost_usd: number | null;
  stop_reason: string | null;
  /** Model we asked for. */
  model: string | null;
  /** Model OpenAI reports it actually served (may be a dated snapshot). */
  served_model: string | null;
  ttft_ms: number | null;
  latency_ms: number | null;
  sources: Source[];
  web_searches: number | null;
  created_at: string;
}

export interface Conversation {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface ConversationDetail extends Conversation {
  messages: Message[];
  total_cost_usd: number;
  total_tokens: number;
  total_input_tokens: number;
  total_cached_input_tokens: number;
  total_output_tokens: number;
  /** Replies with token usage but no price: the total cost is a lower bound. */
  unpriced_messages: number;
}

export interface Usage {
  input_tokens: number;
  cached_input_tokens: number | null;
  output_tokens: number;
  reasoning_tokens: number | null;
  total_tokens: number;
}

/** USD, calculated by the backend from the usage above and AI/data/prices.toml. */
export interface CostBreakdown {
  input_cost: number; // non-cached input tokens
  cached_input_cost: number;
  output_cost: number; // includes reasoning tokens
  total_cost: number;
}

// The payloads of the SSE events the server emits (see AI/app/chat/streaming.py).
export interface StartEvent {
  user_message_id: string;
  assistant_message_id: string;
  history_messages: number;
  history_truncated: boolean;
  /** The history opens with a rolling summary of older messages. */
  history_summarized: boolean;
}
export interface DeltaEvent {
  text: string;
}
export interface DoneEvent {
  stop_reason: string;
  usage: Usage | null; // null = OpenAI reported none
  cost: CostBreakdown | null; // null = no price configured for the model
  requested_model: string;
  served_model: string | null;
  ttft_ms: number | null;
  latency_ms: number | null;
  web_searches: number;
  conversation_total_cost_usd: number;
  conversation_total_tokens: number;
  conversation_total_input_tokens: number;
  conversation_total_output_tokens: number;
  unpriced_messages: number;
}
export interface ToolCallEvent {
  id: string;
  name: string;
  query: string | null;
}
export interface ToolResultEvent {
  id: string;
  name: string;
  sources: Source[];
  error: string | null;
}
export interface ErrorEvent {
  message: string;
  partial: boolean;
}
export interface TitleEvent {
  title: string;
}

export interface StreamHandlers {
  onStart?: (e: StartEvent) => void;
  onDelta?: (e: DeltaEvent) => void;
  onToolCall?: (e: ToolCallEvent) => void;
  onToolResult?: (e: ToolResultEvent) => void;
  onDone?: (e: DoneEvent) => void;
  onError?: (e: ErrorEvent) => void;
  onTitle?: (e: TitleEvent) => void;
}
