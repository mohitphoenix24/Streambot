import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { getInfo } from "./api";

export interface RateInfo {
  rate: number; // INR per 1 USD
  source: "live" | "fallback" | "fixed";
  asOf: string | null;
}

// The USD->INR display rate comes from the backend (live, cached, or fixed via
// USD_TO_INR), so there is one place that owns it. null = not loaded yet, or INR
// hidden: the UI then shows USD only.
const RateContext = createContext<RateInfo | null>(null);
const WebSearchContext = createContext(false);
const AttachmentsContext = createContext(false);

export function RateProvider({ children }: { children: ReactNode }) {
  const [info, setInfo] = useState<RateInfo | null>(null);
  const [searchAvailable, setSearchAvailable] = useState(false);
  const [attachmentsAvailable, setAttachmentsAvailable] = useState(false);
  useEffect(() => {
    getInfo()
      .then((i) => {
        setSearchAvailable(i.web_search_available);
        setAttachmentsAvailable(i.attachments_available);
        setInfo(
          i.usd_to_inr && i.usd_to_inr_source !== "off"
            ? { rate: i.usd_to_inr, source: i.usd_to_inr_source as RateInfo["source"], asOf: i.usd_to_inr_as_of }
            : null,
        );
      })
      .catch(() => setInfo(null)); // INR is a nicety; never block the app on it
  }, []);
  return (
    <RateContext.Provider value={info}>
      <WebSearchContext.Provider value={searchAvailable}>
        <AttachmentsContext.Provider value={attachmentsAvailable}>{children}</AttachmentsContext.Provider>
      </WebSearchContext.Provider>
    </RateContext.Provider>
  );
}

/** Can the server search the web (is a Tavily key configured)? */
export const useWebSearchAvailable = () => useContext(WebSearchContext);
/** Can chats take file attachments? */
export const useAttachmentsAvailable = () => useContext(AttachmentsContext);
export const useRateInfo = () => useContext(RateContext);
export const useUsdToInr = () => useContext(RateContext)?.rate ?? null;

/** Human explanation for tooltips, e.g. "₹96.38 per $1 (live rate, as of …)". */
export function describeRate(info: RateInfo | null): string {
  if (!info) return "";
  const kind = { live: "live rate", fallback: "approximate fallback rate (live lookup failed)", fixed: "fixed rate from USD_TO_INR" }[info.source];
  return `₹${info.rate.toFixed(2)} per $1 (${kind}${info.asOf ? `, as of ${info.asOf}` : ""}). Display conversion only; billing is in USD.`;
}
