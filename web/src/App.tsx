import { useEffect, useState } from "react";
import { api } from "./api";
import { ChatPanel } from "./components/ChatPanel";
import { ToastProvider, useAsync } from "./components/ui";
import { Link, useRoute } from "./hooks/useRoute";
import { EvalsPage } from "./pages/EvalsPage";
import { MetricsPage } from "./pages/MetricsPage";
import { SessionPage } from "./pages/SessionPage";
import { SessionsPage } from "./pages/SessionsPage";

const CHAT_OPEN_KEY = "cu:chat:open";

export default function App() {
  const [route] = useRoute();
  const config = useAsync(() => api.config(), []);
  const backends = useAsync(() => api.backends(), []);
  const available = (backends.data ?? []).filter((b) => b.available).map((b) => b.id);
  const [chatOpen, setChatOpen] = useState(() => {
    try {
      return localStorage.getItem(CHAT_OPEN_KEY) === "1";
    } catch {
      return false;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(CHAT_OPEN_KEY, chatOpen ? "1" : "0");
    } catch {
      /* private mode */
    }
  }, [chatOpen]);
  return (
    <ToastProvider>
      <div className={`app ${chatOpen ? "with-chat" : ""}`}>
        <aside className="sidebar">
          <Link to="/" className="brand">
            <span className="logo" aria-hidden>
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                <rect x="3" y="4" width="18" height="12" rx="2" />
                <path d="M8 20h8" />
              </svg>
            </span>
            computeruse
          </Link>
          <nav className="nav" aria-label="Primary">
            <Link to="/" className={route.name === "sessions" || route.name === "session" ? "active" : ""}>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                <rect x="3" y="4" width="18" height="16" rx="2.5" />
                <path d="M3 9h18M8 14l2.5 2L14 12" />
              </svg>
              Sessions
            </Link>
            <Link to="/metrics" className={route.name === "metrics" ? "active" : ""}>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                <path d="M4 20V11M10 20V4M16 20v-7M3 20h19" />
              </svg>
              Metrics
            </Link>
            <Link to="/evals" className={route.name === "evals" ? "active" : ""}>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                <circle cx="12" cy="12" r="8.5" />
                <path d="M8.5 12.5l2.3 2.3 4.7-5" />
              </svg>
              Evals
            </Link>
          </nav>
          <span className="divider" aria-hidden />
          <button
            className={`btn ghost chat-toggle ${chatOpen ? "toggled" : ""}`}
            onClick={() => setChatOpen((v) => !v)}
            aria-pressed={chatOpen}
            title="Chat with an Antigravity model about the agent or the session on screen"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <path d="M21 12a8 8 0 0 1-8 8H7l-4 3V12a8 8 0 0 1 8-8h2a8 8 0 0 1 8 8z" />
            </svg>
            Chat
          </button>
          <span className="spacer" />
          <div className="env" title="Computers the server can drive right now">
            {available.length > 0 && <span className="computers">computers: {available.join(", ")}</span>}
            {config.data && (
              <span
                className={`pill ${config.data.models_available ? "ok" : "warn"}`}
                title={
                  config.data.models_available
                    ? Object.entries(config.data.model_access.providers).map(([p, st]) => `${p}: ${st.reason}`).join("\n")
                    : "No model provider configured: scripted/replay only"
                }
              >
                {config.data.models_available
                  ? `${config.data.models.find((m) => m.id === config.data!.model)?.provider ?? "model"} · ${config.data.model}`
                  : "no model access"}
              </span>
            )}
            <a className="dim" href="/api/docs" target="_blank" rel="noreferrer" title="OpenAPI docs">
              API docs ↗
            </a>
          </div>
        </aside>
        <div className="body">
          <main>
            {route.name === "sessions" && <SessionsPage />}
            {route.name === "session" && <SessionPage key={route.id} id={route.id} />}
            {route.name === "metrics" && <MetricsPage />}
            {route.name === "evals" && <EvalsPage runId={route.runId} />}
          </main>
          {chatOpen && <ChatPanel sessionId={route.name === "session" ? route.id : undefined} onClose={() => setChatOpen(false)} />}
        </div>
      </div>
    </ToastProvider>
  );
}
