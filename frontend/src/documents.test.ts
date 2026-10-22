import { describe, expect, it } from "vitest";
import { anyProcessing, checkFile, describeDocument, formatBytes, hasFiles, upsertDocument } from "./documents";
import type { DocumentInfo } from "./types";

const doc = (over: Partial<DocumentInfo> = {}): DocumentInfo => ({
  id: "d1",
  conversation_id: "c1",
  filename: "faq.pdf",
  kind: "pdf",
  size_bytes: 2048,
  status: "ready",
  error: null,
  page_count: 3,
  chunk_count: 5,
  created_at: "2026-01-01T00:00:00Z",
  ...over,
});

describe("checkFile", () => {
  it.each(["a.pdf", "A.PDF", "notes.docx", "n.txt", "README.md", "x.markdown", "my.report.v2.pdf"])("accepts %s", (name) => {
    expect(checkFile(name)).toBeNull();
  });
  it.each(["a.exe", "photo.png", "archive.zip", "noextension", "page.html", ".pdf.exe"])("rejects %s", (name) => {
    expect(checkFile(name)).toMatch(/not supported/);
  });
  it("explains old Word files", () => {
    expect(checkFile("old.doc")).toMatch(/\.docx/);
  });
});

describe("formatBytes", () => {
  it("scales units", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2.0 KB");
    expect(formatBytes(200 * 1024)).toBe("200 KB");
    expect(formatBytes(5 * 1024 * 1024)).toBe("5.0 MB");
  });
});

describe("list helpers", () => {
  it("detects documents still being indexed", () => {
    expect(anyProcessing([doc(), doc({ id: "d2", status: "processing" })])).toBe(true);
    expect(anyProcessing([doc(), doc({ id: "d2", status: "failed" })])).toBe(false);
    expect(anyProcessing([])).toBe(false);
  });
  it("upsert appends new documents and replaces known ones in place", () => {
    const a = doc({ id: "a", status: "processing" });
    const b = doc({ id: "b" });
    expect(upsertDocument([a], b).map((d) => d.id)).toEqual(["a", "b"]);
    const updated = upsertDocument([a, b], { ...a, status: "ready" });
    expect(updated.map((d) => d.id)).toEqual(["a", "b"]);
    expect(updated[0].status).toBe("ready");
  });
});

describe("describeDocument", () => {
  it("covers every state", () => {
    expect(describeDocument(doc({ status: "processing" }))).toMatch(/indexing/);
    expect(describeDocument(doc({ status: "failed", error: "No readable text was found in this file." }))).toMatch(/No readable text/);
    expect(describeDocument(doc())).toBe("faq.pdf: ready (3 pages, 5 passages, 2.0 KB)");
    expect(describeDocument(doc({ page_count: null, chunk_count: 1, kind: "text" }))).toMatch(/1 passage,/);
  });
});

describe("hasFiles", () => {
  it("is true only for file drags", () => {
    expect(hasFiles(["Files"])).toBe(true);
    expect(hasFiles(["text/plain"])).toBe(false);
    expect(hasFiles(undefined)).toBe(false);
  });
});
