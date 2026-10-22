// Small pure helpers for per-chat attachments (kept out of the components so they can be unit-tested).
import type { DocumentInfo } from "./types";

/** What the file picker offers. The server re-checks everything: this only saves a round trip. */
export const ACCEPT = ".pdf,.docx,.txt,.md,.markdown";
const EXTENSIONS = ["pdf", "docx", "txt", "md", "markdown"];

/** null when the file looks acceptable, otherwise a message for the user. */
export function checkFile(name: string): string | null {
  const ext = name.includes(".") ? name.split(".").pop()!.toLowerCase() : "";
  if (ext === "doc") return "Old .doc files are not supported. Save the file as .docx and try again.";
  if (!EXTENSIONS.includes(ext)) return `“${name}” is not supported. Attach a PDF, Word (.docx), text or Markdown file.`;
  return null;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Is any document still being indexed? (Then the list should be polled.) */
export const anyProcessing = (docs: DocumentInfo[]) => docs.some((d) => d.status === "processing");

/** Insert a document, or replace the one with the same id (keeps upload order). */
export function upsertDocument(docs: DocumentInfo[], doc: DocumentInfo): DocumentInfo[] {
  return docs.some((d) => d.id === doc.id) ? docs.map((d) => (d.id === doc.id ? doc : d)) : [...docs, doc];
}

/** One line for a chip's tooltip. */
export function describeDocument(doc: DocumentInfo): string {
  if (doc.status === "processing") return `${doc.filename}: reading and indexing…`;
  if (doc.status === "failed") return `${doc.filename}: ${doc.error ?? "could not be processed"}`;
  const pages = doc.page_count ? `${doc.page_count} ${doc.page_count === 1 ? "page" : "pages"}, ` : "";
  return `${doc.filename}: ready (${pages}${doc.chunk_count} ${doc.chunk_count === 1 ? "passage" : "passages"}, ${formatBytes(doc.size_bytes)})`;
}

/** Only real file drags: dragging selected text over the page must not show the drop target. */
export const hasFiles = (types: readonly string[] | undefined) => !!types && Array.from(types).includes("Files");
