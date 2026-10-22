import { useEffect, useState } from "react";
import { getUsage } from "./api";
import { axisTicks, barPercent, compactNumber, formatUsdAxis, labelIndexes, niceMax, shortDate } from "./chart";
import { formatCost, formatInr } from "./format";
import { useRateInfo, useUsdToInr, describeRate } from "./rate";
import type { UsageReport } from "./types";

const RANGES = [7, 30, 90] as const;
const PURPOSE_LABEL = { reply: "Replies", title: "Chat titles", summary: "Memory summaries" } as const;

/** "$0.0124 · ₹1.17" as two parts so the hero figure can put INR on its own line. */
function useMoney() {
  const rate = useUsdToInr();
  return {
    usd: (v: number) => formatCost(v),
    inr: (v: number) => (rate ? formatInr(v, rate) : null),
    both: (v: number) => (rate ? `${formatCost(v)} · ${formatInr(v, rate)}` : formatCost(v)),
  };
}

export default function Usage() {
  const [days, setDays] = useState<(typeof RANGES)[number]>(30);
  const [data, setData] = useState<UsageReport | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");

  useEffect(() => {
    let current = true;
    setState((s) => (s === "ready" ? s : "loading")); // refetching keeps the previous frame on screen
    // The browser's own offset (east positive), so "today" matches the user's clock.
    getUsage(days, -new Date().getTimezoneOffset())
      .then((report) => {
        if (current) {
          setData(report);
          setState("ready");
        }
      })
      .catch(() => current && setState("error"));
    return () => {
      current = false;
    };
  }, [days]);

  return (
    <section className="usage">
      <header className="usage-head">
        <div>
          <h1>Usage</h1>
          {data && (
            <p className="usage-sub">
              {shortDate(data.from)} – {shortDate(data.to)} · spend is computed from the usage OpenAI reported
            </p>
          )}
        </div>
        <div className="range" role="radiogroup" aria-label="Date range">
          {RANGES.map((r) => (
            <button key={r} role="radio" aria-checked={days === r} className={days === r ? "on" : ""} onClick={() => setDays(r)}>
              {r} days
            </button>
          ))}
        </div>
      </header>

      {state === "error" && <div className="state state-error">Could not load usage. Is the backend running?</div>}
      {state === "loading" && !data && <div className="state">Loading usage…</div>}
      {data && (
        <div className={`usage-body ${state === "loading" ? "refetching" : ""}`}>
          {data.totals.calls === 0 ? <Empty days={days} /> : <Report data={data} />}
        </div>
      )}
    </section>
  );
}

function Empty({ days }: { days: number }) {
  return (
    <div className="state empty-usage">
      <h3>No usage in the last {days} days</h3>
      <p>Send a message and its tokens and cost will show up here.</p>
    </div>
  );
}

function Report({ data }: { data: UsageReport }) {
  const money = useMoney();
  const rateInfo = useRateInfo();
  const t = data.totals;
  const hit = data.cache.hit_rate;

  return (
    <>
      {/* KPI row: the hero figure leads; everything else is a stat tile */}
      <div className="kpis">
        <div className="kpi hero" title={describeRate(rateInfo)}>
          <span className="kpi-label">Total spend</span>
          <span className="hero-value">{money.usd(t.cost_usd)}</span>
          {money.inr(t.cost_usd) && <span className="hero-inr">{money.inr(t.cost_usd)}</span>}
          <span className="kpi-note">
            {t.replies.toLocaleString()} {t.replies === 1 ? "reply" : "replies"} · {t.calls - t.replies} background {t.calls - t.replies === 1 ? "call" : "calls"}
          </span>
        </div>

        <div className="kpi">
          <span className="kpi-label">Tokens</span>
          <span className="kpi-value">{compactNumber(t.total_tokens)}</span>
          <span className="kpi-note">
            {compactNumber(t.input_tokens)} input tokens · {compactNumber(t.output_tokens)} output tokens
          </span>
        </div>

        <div className="kpi">
          <span className="kpi-label">Cache hit rate</span>
          <span className="kpi-value">{hit === null ? "–" : `${(hit * 100).toFixed(0)}%`}</span>
          <div className="meter" role="img" aria-label={hit === null ? "no data" : `${(hit * 100).toFixed(0)} percent of input tokens came from the cache`}>
            <span style={{ width: `${(hit ?? 0) * 100}%` }} />
          </div>
          <span className="kpi-note">{compactNumber(data.cache.cached_input_tokens)} of {compactNumber(t.input_tokens)} input tokens cached</span>
        </div>

        <div className="kpi">
          <span className="kpi-label">Saved by caching</span>
          <span className="kpi-value accent">{data.cache.savings_usd === null ? "n/a" : money.usd(data.cache.savings_usd)}</span>
          <span className="kpi-note">
            {data.cache.savings_usd !== null && money.inr(data.cache.savings_usd) ? `${money.inr(data.cache.savings_usd)} · ` : ""}
            vs. paying the normal input rate
          </span>
        </div>

        <div className="kpi">
          <span className="kpi-label">Web searches</span>
          <span className="kpi-value">{t.web_searches.toLocaleString()}</span>
          <span className="kpi-note">counted, not priced</span>
        </div>
      </div>

      {t.unpriced_calls > 0 && (
        <div className="notice notice-warn">
          {t.unpriced_calls} {t.unpriced_calls === 1 ? "call has" : "calls have"} no price in <code>AI/data/prices.toml</code>, so spend is a lower bound.
        </div>
      )}

      <DailySpend daily={data.daily} />

      <div className="usage-cols">
        <div className="panel">
          <h2>Where it goes</h2>
          <ul className="hbars">
            {(() => {
              const max = Math.max(...data.by_purpose.map((p) => p.cost_usd), 0);
              return data.by_purpose.map((p) => (
                <li key={p.purpose}>
                  <div className="hbar-row">
                    <span>{PURPOSE_LABEL[p.purpose]}</span>
                    <span className="hbar-val">{money.both(p.cost_usd)}</span>
                  </div>
                  <div className="hbar-track">
                    <span className="hbar" style={{ width: `${barPercent(p.cost_usd, max)}%` }} />
                  </div>
                  <span className="hbar-note">{p.calls.toLocaleString()} {p.calls === 1 ? "call" : "calls"} · {compactNumber(p.total_tokens)} tokens</span>
                </li>
              ));
            })()}
          </ul>
        </div>

        <div className="panel">
          <h2>By model</h2>
          <table className="usage-table">
            <thead>
              <tr>
                <th>Model</th>
                <th>Calls</th>
                <th>Tokens</th>
                <th>Spend</th>
              </tr>
            </thead>
            <tbody>
              {data.by_model.map((m) => (
                <tr key={m.model}>
                  <td>{m.model}</td>
                  <td>{m.calls.toLocaleString()}</td>
                  <td>{compactNumber(m.total_tokens)}</td>
                  <td>{money.usd(m.cost_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {data.top_conversations.length > 0 && (
        <div className="panel">
          <h2>Most expensive chats</h2>
          <ol className="top-chats">
            {data.top_conversations.map((c) => (
              <li key={c.id}>
                <a href={`#/c/${c.id}`}>
                  <span className="top-title">{c.title}</span>
                  <span className="top-meta">{c.replies} {c.replies === 1 ? "reply" : "replies"}</span>
                  <span className="top-cost">{money.both(c.cost_usd)}</span>
                </a>
              </li>
            ))}
          </ol>
        </div>
      )}
    </>
  );
}

/** One series, so one color and no legend box: the heading names it. */
function DailySpend({ daily }: { daily: UsageReport["daily"] }) {
  const money = useMoney();
  const [asTable, setAsTable] = useState(false);
  const [hover, setHover] = useState<number | null>(null);

  const max = Math.max(...daily.map((d) => d.cost_usd), 0);
  const top = niceMax(max);
  const ticks = axisTicks(max);
  const peak = max > 0 ? daily.findIndex((d) => d.cost_usd === max) : -1;
  const labels = new Set(labelIndexes(daily.length));

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>Daily spend</h2>
        <button className="link-btn" onClick={() => setAsTable((v) => !v)}>
          {asTable ? "Show chart" : "Show table"}
        </button>
      </div>

      {asTable ? (
        <table className="usage-table">
          <thead>
            <tr>
              <th>Day</th>
              <th>Replies</th>
              <th>Tokens</th>
              <th>Spend</th>
            </tr>
          </thead>
          <tbody>
            {[...daily].reverse().map((d) => (
              <tr key={d.date}>
                <td>{shortDate(d.date)}</td>
                <td>{d.replies}</td>
                <td>{compactNumber(d.total_tokens)}</td>
                <td>{money.both(d.cost_usd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div className="chart" onPointerLeave={() => setHover(null)}>
          <div className="chart-y" aria-hidden>
            {ticks.map((v) => (
              <span key={v} style={{ bottom: `${(v / top) * 100}%` }}>
                {formatUsdAxis(v)}
              </span>
            ))}
          </div>
          <div className="chart-plot">
            {ticks.map((v) => (
              <span key={v} className="grid" style={{ bottom: `${(v / top) * 100}%` }} aria-hidden />
            ))}
            <div className="bars">
              {daily.map((d, i) => (
                <div
                  key={d.date}
                  className={`slot ${hover === i ? "hover" : ""}`}
                  tabIndex={0}
                  role="img"
                  aria-label={`${shortDate(d.date)}: ${money.usd(d.cost_usd)}, ${d.replies} replies`}
                  onPointerEnter={() => setHover(i)}
                  onFocus={() => setHover(i)}
                  onBlur={() => setHover(null)}
                >
                  {d.cost_usd > 0 && <span className="bar" style={{ height: `${barPercent(d.cost_usd, top)}%` }} />}
                  {i === peak && (
                    <span className="peak" style={{ bottom: `calc(${barPercent(d.cost_usd, top)}% + 4px)` }}>
                      {formatUsdAxis(d.cost_usd)}
                    </span>
                  )}
                </div>
              ))}
            </div>
            {hover !== null && (
              <div
                className={`tip ${hover > daily.length * 0.6 ? "tip-flip" : ""}`}
                style={{ left: `${((hover + 0.5) / daily.length) * 100}%` }}
                role="status"
              >
                <span className="tip-date">{shortDate(daily[hover].date)}</span>
                <span className="tip-row">
                  <i /> <b>{money.both(daily[hover].cost_usd)}</b>
                </span>
                <span className="tip-meta">
                  {daily[hover].replies} {daily[hover].replies === 1 ? "reply" : "replies"} · {compactNumber(daily[hover].total_tokens)} tokens
                </span>
              </div>
            )}
          </div>
          <div className="chart-x" aria-hidden>
            {daily.map((d, i) => (
              <span key={d.date}>{labels.has(i) ? shortDate(d.date) : ""}</span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
