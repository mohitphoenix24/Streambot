// Models cite web sources inline as [1], [2], or [1, 2]. Turn those markers into links
// to a #cite-N fragment, which Message.tsx renders as numbered chips pointing at the
// real source. Only numbers that exist among the reply's sources become links, and
// code (fenced or inline) is left alone, like math.ts.
export function linkCitations(text: string, sourceCount: number): string {
  if (sourceCount < 1) return text;
  const link = (n: number) => (n >= 1 && n <= sourceCount ? `[${n}](#cite-${n})` : `[${n}]`);
  return text
    .split(/(```[\s\S]*?(?:```|$)|`[^`\n]*`)/g)
    .map((part, i) =>
      i % 2 === 1
        ? part // a code segment
        : part
            // [1, 2] -> [1][2] as separate links
            .replace(/\[(\d{1,2}(?:\s*,\s*\d{1,2})+)\]/g, (_, list: string) =>
              list.split(",").map((n) => link(Number(n))).join(""),
            )
            // [2][3] -> two links (back-to-back markers are common)
            .replace(/(?:\[\d{1,2}\]){2,}(?![(:])/g, (run: string) =>
              run.replace(/\[(\d{1,2})\]/g, (_, n: string) => link(Number(n))),
            )
            // [3] -> link, unless it is already a link label ([x][3]) or has a target ([3](...), [3]:)
            .replace(/(?<![\]\w])\[(\d{1,2})\](?![(:\[])/g, (_, n: string) => link(Number(n))),
    )
    .join("");
}
