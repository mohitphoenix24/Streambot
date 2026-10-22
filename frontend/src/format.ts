/** Dollar amounts from LLM calls are tiny (a fraction of a cent), so show more precision. */
export function formatCost(cost: number | null | undefined): string {
  if (cost === null || cost === undefined) return "cost n/a";
  if (cost === 0) return "$0";
  return cost < 0.01 ? `$${cost.toFixed(6)}` : `$${cost.toFixed(4)}`;
}

export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "–";
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(2)} s`;
}

/** INR equivalent of a USD amount at a manually configured rate (display only). */
export function formatInr(usd: number, rate: number): string {
  const inr = usd * rate;
  if (inr === 0) return "₹0";
  return inr < 1 ? `₹${inr.toFixed(4)}` : `₹${inr.toFixed(2)}`;
}

/** "$0.000524 · ₹0.0461" side by side; INR only when a rate is configured. */
export function formatMoney(usd: number | null | undefined, rate: number | null): string {
  if (usd === null || usd === undefined) return "cost n/a";
  return rate ? `${formatCost(usd)} · ${formatInr(usd, rate)}` : formatCost(usd);
}
