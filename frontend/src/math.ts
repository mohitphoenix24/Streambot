/**
 * Models write LaTeX as \( inline \) and \[ display \]. remark-math understands
 * $$ ... $$, so rewrite the delimiters, leaving code (fenced and inline) untouched.
 */
export function normalizeMath(text: string): string {
  return text
    .split(/(```[\s\S]*?(?:```|$)|`[^`\n]*`)/g)
    .map((part, i) =>
      i % 2 === 1
        ? part // a code segment
        : part
            .replace(/\\\[([\s\S]+?)\\\]/g, (_, m: string) => `\n\n$$\n${m.trim()}\n$$\n\n`)
            .replace(/\\\(([\s\S]+?)\\\)/g, (_, m: string) => `$$${m.trim()}$$`),
    )
    .join("");
}
