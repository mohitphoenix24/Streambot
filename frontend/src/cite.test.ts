import { describe, expect, it } from "vitest";
import { linkCitations } from "./cite";

describe("linkCitations", () => {
  it("links markers that match a source", () => {
    expect(linkCitations("Out now [1] and [2].", 2)).toBe("Out now [1](#cite-1) and [2](#cite-2).");
  });
  it("leaves numbers without a source alone", () => {
    expect(linkCitations("see [3]", 2)).toBe("see [3]");
  });
  it("does nothing when there are no sources", () => {
    expect(linkCitations("array[1] and [1]", 0)).toBe("array[1] and [1]");
  });
  it("splits grouped citations", () => {
    expect(linkCitations("facts [1, 2]", 2)).toBe("facts [1](#cite-1)[2](#cite-2)");
  });
  it("links back-to-back markers", () => {
    expect(linkCitations("both [2][3] agree", 3)).toBe("both [2](#cite-2)[3](#cite-3) agree");
  });
  it("leaves code alone", () => {
    const code = "```py\nx = a[1]\n```\nand `b[2]`";
    expect(linkCitations(code, 2)).toBe(code);
  });
  it("does not touch indexing right after a word or existing links", () => {
    expect(linkCitations("arr[1] [link](http://x) [1](http://y)", 2)).toBe("arr[1] [link](http://x) [1](http://y)");
  });
});
