import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, deleteConversation, listConversations } from "./api";
import Chat from "./Chat";
import { CheckIcon, LogoMark, MenuIcon, PlusIcon, SearchIcon, TrashIcon } from "./Icons";
import { RateProvider } from "./rate";
import type { Conversation } from "./types";

// The selected conversation lives in the URL hash (#/c/<id>), so a page refresh,
// a bookmark, and the back button all land on the same conversation. The
// conversation CONTENT comes from the database via the API.
const idFromHash = () => /^#\/c\/(.+)$/.exec(window.location.hash)?.[1] ?? null;

// Opening the app in a new tab or window always starts a NEW chat, like ChatGPT, even if the
// address still carries an old #/c/<id>. A refresh inside the same tab keeps its chat:
// sessionStorage lives exactly as long as the tab, so "no marker yet" means a fresh launch.
(function startNewChatOnLaunch() {
  try {
    if (sessionStorage.getItem("chat-tab-started")) return;
    sessionStorage.setItem("chat-tab-started", "1");
    if (window.location.hash) {
      history.replaceState(null, "", window.location.pathname + window.location.search);
    }
  } catch {
    // Storage blocked: fall back to keeping whatever the URL says.
  }
})();

const DAY_MS = 86_400_000;

/** Bucket conversations (already sorted newest first) into Today / Yesterday / ... */
function groupByDay(items: Conversation[]): { label: string; items: Conversation[] }[] {
  const startOfToday = new Date().setHours(0, 0, 0, 0);
  const buckets: [string, (t: number) => boolean][] = [
    ["Today", (t) => t >= startOfToday],
    ["Yesterday", (t) => t >= startOfToday - DAY_MS],
    ["Previous 7 days", (t) => t >= startOfToday - 7 * DAY_MS],
    ["Older", () => true],
  ];
  const groups = buckets.map(([label]) => ({ label, items: [] as Conversation[] }));
  for (const c of items) {
    const t = new Date(c.updated_at).getTime();
    groups[buckets.findIndex(([, test]) => test(t))].items.push(c);
  }
  return groups.filter((g) => g.items.length > 0);
}

export default function App() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [listState, setListState] = useState<"loading" | "ready" | "error">("loading");
  const [selectedId, setSelectedId] = useState<string | null>(idFromHash);
  const [sidebarOpen, setSidebarOpen] = useState(false); // only matters on narrow screens
  // Remounts Chat when it changes. A draft chat that gets saved keeps its key, so the
  // reply that is streaming into it is not interrupted when the URL gains an id.
  const [chatKey, setChatKey] = useState(() => idFromHash() ?? "draft-0");
  const savedDraftId = useRef<string | null>(null);
  const draftCount = useRef(0);
  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  const refresh = useCallback(async () => {
    try {
      setConversations(await listConversations());
      setListState("ready");
    } catch {
      setListState("error");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    const onHash = () => {
      const id = idFromHash();
      setSelectedId(id);
      if (id !== null && id === savedDraftId.current) {
        savedDraftId.current = null; // the URL just caught up with a draft that was saved
        return;
      }
      setChatKey(id ?? `draft-${++draftCount.current}`);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const select = (id: string) => {
    window.location.hash = `/c/${id}`;
    setSidebarOpen(false);
  };

  // "New chat" only opens an empty draft; nothing is created on the server until
  // the first message is sent (see Chat).
  function newChat() {
    setQuery("");
    setSidebarOpen(false);
    if (window.location.hash) window.location.hash = ""; // hashchange swaps in a fresh draft
    else setChatKey(`draft-${++draftCount.current}`);
  }

  const onCreated = useCallback((conv: Conversation) => {
    savedDraftId.current = conv.id;
    setConversations((cs) => [conv, ...cs]);
    window.location.hash = `/c/${conv.id}`;
  }, []);

  async function confirmDelete(id: string) {
    setConfirmingId(null);
    try {
      await deleteConversation(id);
    } catch (err) {
      // 404 = already gone, which is what the user wanted; anything else is shown.
      if (!(err instanceof ApiError && err.status === 404)) {
        setDeleteError(err instanceof ApiError ? err.message : "Could not delete the chat.");
        return;
      }
    }
    setDeleteError(null);
    setConversations((cs) => cs.filter((c) => c.id !== id));
    if (id === selectedId) newChat();
    void refresh();
  }

  const onActivity = useCallback(() => {
    void refresh();
  }, [refresh]);

  const onTitle = useCallback((id: string, title: string) => {
    setConversations((cs) => cs.map((c) => (c.id === id ? { ...c, title } : c)));
  }, []);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return q ? conversations.filter((c) => c.title.toLowerCase().includes(q)) : conversations;
  }, [conversations, query]);
  const groups = useMemo(() => groupByDay(filtered), [filtered]);

  return (
    <RateProvider>
      <div className="app">
        <button className="menu-btn" onClick={() => setSidebarOpen((o) => !o)} aria-label="Toggle conversations">
          <MenuIcon />
        </button>
        {sidebarOpen && <div className="scrim" onClick={() => setSidebarOpen(false)} />}

        <aside className={`sidebar ${sidebarOpen ? "open" : ""}`}>
          <div className="brand">
            <LogoMark />
            <span className="brand-name">
              Streaming<span>Chat</span>
            </span>
          </div>

          <div className="sidebar-actions">
            <button className="btn btn-new" onClick={newChat}>
              <PlusIcon /> New chat
            </button>
            {conversations.length > 4 && (
              <label className="search">
                <SearchIcon />
                <input
                  type="search"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Search chats"
                  aria-label="Search conversations"
                />
              </label>
            )}
          </div>

          <nav className="conv-list" aria-label="Conversations">
            {listState === "loading" && (
              <div className="skeletons" aria-label="Loading conversations">
                <span /> <span /> <span />
              </div>
            )}
            {listState === "error" && (
              <div className="state state-error">
                Can't reach the server.{" "}
                <button className="link" onClick={() => void refresh()}>
                  Retry
                </button>
              </div>
            )}
            {deleteError && (
              <div className="state state-error" role="alert">
                {deleteError}
              </div>
            )}
            {listState === "ready" && conversations.length === 0 && (
              <div className="state">No conversations yet.</div>
            )}
            {listState === "ready" && conversations.length > 0 && filtered.length === 0 && (
              <div className="state">No chats match “{query}”.</div>
            )}
            {groups.map((g) => (
              <section key={g.label} className="conv-group">
                <h2>{g.label}</h2>
                {g.items.map((c) =>
                  c.id === confirmingId ? (
                    <div key={c.id} className="conv-confirm" role="alertdialog" aria-label={`Delete ${c.title}?`}>
                      <span>Delete this chat?</span>
                      <button className="confirm-yes" onClick={() => void confirmDelete(c.id)} autoFocus>
                        <CheckIcon /> Delete
                      </button>
                      <button className="confirm-no" onClick={() => setConfirmingId(null)}>
                        Cancel
                      </button>
                    </div>
                  ) : (
                    <div key={c.id} className={`conv-row ${c.id === selectedId ? "active" : ""}`}>
                      <button
                        className="conv-item"
                        onClick={() => select(c.id)}
                        title={c.title}
                        aria-current={c.id === selectedId ? "page" : undefined}
                      >
                        {c.title}
                      </button>
                      <button
                        className="conv-delete"
                        onClick={() => setConfirmingId(c.id)}
                        title="Delete chat"
                        aria-label={`Delete chat: ${c.title}`}
                      >
                        <TrashIcon />
                      </button>
                    </div>
                  ),
                )}
              </section>
            ))}
          </nav>
        </aside>

        <main className="main">
          {/* key => a fresh Chat (fresh state, aborted stream) per conversation or new draft */}
          <Chat key={chatKey} conversationId={selectedId} onCreated={onCreated} onTitle={onTitle} onActivity={onActivity} />
        </main>
      </div>
    </RateProvider>
  );
}
