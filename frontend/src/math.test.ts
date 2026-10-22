import { describe, expect, it } from "vitest";
import { normalizeMath } from "./math";

describe("normalizeMath", () => {
  it("rewrites inline \\( \\) to $$ $$", () => {
    expect(normalizeMath("Use \\(x = 2\\) here")).toBe("Use $$x = 2$$ here");
  });
  it("rewrites display \\[ \\] to a $$ block on its own lines", () => {
    expect(normalizeMath("Range:\\[ R=\\frac{u^2}{g} \\]done")).toBe("Range:\n\n$$\nR=\\frac{u^2}{g}\n$$\n\ndone");
  });
  it("leaves fenced and inline code alone", () => {
    const code = "```py\nprint('\\(x\\)')\n```\nand `\\[y\\]`";
    expect(normalizeMath(code)).toBe(code);
  });
  it("leaves an unterminated code fence (mid-answer) alone", () => {
    const open = "```py\nre.sub('\\(a\\)', x)";
    expect(normalizeMath(open)).toBe(open);
  });
  it("does not touch dollar amounts", () => {
    expect(normalizeMath("costs $5 and $6")).toBe("costs $5 and $6");
  });
});
