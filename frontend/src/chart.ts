// Small, pure helpers for the Usage page's charts (kept out of the component so they can be tested).

/** Round `max` up to a clean axis ceiling: 1, 2, 2.5, 5 or 10 times a power of ten. */
export function niceMax(max: number): number {
  if (!(max > 0)) return 1;
  const exp = 10 ** Math.floor(Math.log10(max));
  const f = max / exp;
  const nice = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10;
  return nice * exp;
}

/** Evenly spaced tick values from 0 up to the nice ceiling (inclusive), `steps` intervals. */
export function axisTicks(max: number, steps = 4): number[] {
  const top = niceMax(max);
  return Array.from({ length: steps + 1 }, (_, i) => (top * i) / steps);
}

/** Axis label for a USD amount. LLM costs are fractions of a cent, so keep two significant digits. */
export function formatUsdAxis(v: number): string {
  if (v === 0) return "$0";
  if (v >= 100) return `$${Math.round(v)}`;
  if (v >= 1) return `$${parseFloat(v.toFixed(2))}`;
  return `$${parseFloat(v.toPrecision(2))}`;
}

/** 12400 -> "12.4K", 3_200_000 -> "3.2M". Under 1,000 stays exact. */
export function compactNumber(n: number): string {
  if (n >= 1_000_000) return `${parseFloat((n / 1_000_000).toFixed(1))}M`;
  if (n >= 10_000) return `${parseFloat((n / 1000).toFixed(1))}K`;
  return n.toLocaleString();
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2026-10-22" -> "Oct 22". Parsed by hand: `new Date("2026-10-22")` is UTC and can shift a day. */
export function shortDate(iso: string): string {
  const [, m, d] = iso.split("-").map(Number);
  return `${MONTHS[m - 1]} ${d}`;
}

/** Height of a column as a percentage of the axis. A non-zero value always gets a visible sliver. */
export function barPercent(value: number, axisMax: number): number {
  if (!(value > 0) || !(axisMax > 0)) return 0;
  return Math.max((value / axisMax) * 100, 1.5);
}

/** Which x labels to show so they never collide: every k-th, always including the last day. */
export function labelIndexes(count: number, maxLabels = 7): number[] {
  if (count <= maxLabels) return Array.from({ length: count }, (_, i) => i);
  const step = Math.ceil((count - 1) / (maxLabels - 1));
  const out: number[] = [];
  for (let i = count - 1; i >= 0; i -= step) out.unshift(i);
  return out;
}
