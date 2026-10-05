/**
 * Right-hand chat panel: talk to an Antigravity model about the agent, the session on screen, or
 * anything else. Backed by `/api/chat` (one tool-less Antigravity conversation per chat, kept in
 * server memory). When a session page is open, each message can carry the part of that session's
 * timeline the chat has not seen yet plus the latest screenshot — the server assembles it.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { fmtMs, fmtRelative, truncate } from "../lib/format";
import { Markdown } from "../lib/markdown";
import type { Chat, ChatIndex, ChatMessage } from "../types";
import { Spinner, useToast } from "./ui";

const KEYS = { chat: "cu:chat:id", model: "cu:chat:model", share: "cu:chat:share", shot: "cu:chat:screenshot" };

function stored(key: string, fallback: string): string {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

function remember(key: string, value: string | null) {
  try {
    if (value == null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    /* private mode */
  }
}

export function ChatPanel({ sessionId, onClose }: { sessionId?: string; onClose: () => void }) {
  const toast = useToast();
  const [index, setIndex] = useState<ChatIndex | null>(null);
  const [indexError, setIndexError] = useState<string | null>(null);
  const [chat, setChat] = useState<Chat | null>(null);
  const [model, setModel] = useState(() => stored(KEYS.model, ""));
  const [share, setShare] = useState(() => stored(KEYS.share, "1") === "1");
  const [screenshot, setScreenshot] = useState(() => stored(KEYS.shot, "1") === "1");
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [showRecent, setShowRecent] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const refreshIndex = useCallback(async () => {
    try {
      setIndex(await api.chatIndex());
      setIndexError(null);
    } catch (e) {
      setIndexError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  // Boot: availability + models, then the chat used last time (if the server still has it).
  useEffect(() => {
    void refreshIndex();
    const last = stored(KEYS.chat, "");
    if (!last) return;
    api
      .chat(last)
      .then(setChat)
      .catch(() => remember(KEYS.chat, null));
  }, [refreshIndex]);

  // Long-poll while a reply is being generated.
  useEffect(() => {
    if (!chat?.pending) return;
    const id = chat.id;
    const ctrl = new AbortController();
    let alive = true;
    (async () => {
      while (alive) {
        try {
          const next = await api.chat(id, 20, ctrl.signal);
          if (!alive) return;
          setChat(next);
          if (!next.pending) {
            void refreshIndex();
            return;
          }
        } catch (e) {
          if (!alive) return;
          if (e instanceof ApiError && e.status === 404) {
            setChat(null);
            remember(KEYS.chat, null);
            return;
          }
          await new Promise((r) => setTimeout(r, 1500));
        }
      }
    })();
    return () => {
      alive = false;
      ctrl.abort();
    };
  }, [chat?.id, chat?.pending, refreshIndex]);

  // Keep the newest message in view.
  const lastId = chat?.messages[chat.messages.length - 1]?.id;
  const lastPending = chat?.messages[chat.messages.length - 1]?.pending;
  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lastId, lastPending]);

  const models = index?.models ?? [];
  const effectiveModel = useMemo(() => {
    const wanted = model || chat?.model || index?.default_model || "";
    return models.some((m) => m.id === wanted && m.available) ? wanted : (models.find((m) => m.available)?.id ?? "");
  }, [model, chat?.model, index?.default_model, models]);
  const pending = chat?.pending ?? false;
  const canSend = Boolean(index?.available && effectiveModel && draft.trim()) && !pending && !busy;
  const attach = Boolean(sessionId && share);

  const send = async () => {
    const text = draft.trim();
    if (!text || pending || busy || !index?.available) return;
    setBusy(true);
    try {
      let id = chat?.id;
      if (!id) {
        const created = await api.createChat({ model: effectiveModel || undefined });
        id = created.id;
        setChat(created);
        remember(KEYS.chat, id);
      }
      const res = await api.sendChat(id, {
        text,
        model: effectiveModel || undefined,
        session_id: attach ? sessionId : undefined,
        screenshot: attach ? screenshot : undefined,
      });
      setDraft("");
      setChat((c) =>
        c && c.id === id
          ? { ...c, ...res.chat, messages: [...c.messages, res.user_message, res.message] }
          : { ...res.chat, messages: [res.user_message, res.message] },
      );
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      toast(msg, "bad");
      if (e instanceof ApiError && e.status === 404) {
        setChat(null);
        remember(KEYS.chat, null);
      } else if (e instanceof ApiError && e.status === 409 && chat) {
        setChat(await api.chat(chat.id).catch(() => chat));
      }
    } finally {
      setBusy(false);
      inputRef.current?.focus();
    }
  };

  const stop = async () => {
    if (!chat) return;
    try {
      setChat(await api.cancelChat(chat.id));
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "bad");
    }
  };

  const newChat = () => {
    setChat(null);
    remember(KEYS.chat, null);
    setShowRecent(false);
    inputRef.current?.focus();
  };

  const openChat = async (id: string) => {
    try {
      const c = await api.chat(id);
      setChat(c);
      remember(KEYS.chat, id);
      setShowRecent(false);
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "bad");
      void refreshIndex();
    }
  };

  const removeChat = async (id: string) => {
    try {
      await api.deleteChat(id);
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 404)) toast(e instanceof Error ? e.message : String(e), "bad");
    }
    if (chat?.id === id) {
      setChat(null);
      remember(KEYS.chat, null);
    }
    void refreshIndex();
  };

  const pickModel = (id: string) => {
    setModel(id);
    remember(KEYS.model, id);
  };

  const onKey = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send();
    }
  };

  const rows = Math.min(6, Math.max(1, draft.split("\n").length));
  const recent = (index?.chats ?? []).filter((c) => c.message_count > 0 || c.id === chat?.id);
  const messages = chat?.messages ?? [];
  // The list unmounts when it empties; don't leave its toggle lit.
  useEffect(() => {
    if (recent.length === 0) setShowRecent(false);
  }, [recent.length]);

  return (
    <aside className="chat-panel" aria-label="Chat">
      <div className="chat-head">
        <strong>Chat</strong>
        <span className="dim chat-sub" title={index?.reason ?? ""}>
          {index ? (index.available ? "Antigravity" : "unavailable") : "…"}
        </span>
        <span className="spacer" />
        <button className="btn sm ghost" onClick={newChat} disabled={!chat} title="Start a new chat">
          New
        </button>
        <button
          className={`btn sm ghost ${showRecent ? "toggled" : ""}`}
          onClick={() => setShowRecent((v) => !v)}
          disabled={recent.length === 0}
          title="Recent chats (kept while the server runs)"
        >
          Recent{recent.length ? ` · ${recent.length}` : ""}
        </button>
        <button className="btn sm ghost icon" onClick={onClose} aria-label="Close chat" title="Close">
          ✕
        </button>
      </div>

      {showRecent && recent.length > 0 && (
        <div className="chat-recent">
          {recent.map((c) => (
            <div key={c.id} className={`chat-recent-row ${c.id === chat?.id ? "current" : ""}`}>
              <button className="chat-recent-open" onClick={() => void openChat(c.id)} title={c.title ?? c.id}>
                <span className="chat-recent-title">{c.title ?? "(untitled)"}</span>
                <span className="dim">
                  {c.model_label} · {c.message_count} msgs · {fmtRelative(c.updated_at)}
                  {c.pending ? " · replying…" : ""}
                </span>
              </button>
              <button className="btn sm ghost icon" onClick={() => void removeChat(c.id)} aria-label="Delete chat" title="Delete">
                🗑
              </button>
            </div>
          ))}
        </div>
      )}

      <div className="chat-messages" ref={listRef}>
        {indexError && <div className="chat-notice bad">Cannot reach the server: {indexError}</div>}
        {index && !index.available && (
          <div className="chat-notice">
            <strong>Chat needs a running Antigravity.</strong>
            <div className="dim">{index.reason}</div>
            <div className="dim">
              Start the server from an Antigravity terminal (or set <code>COMPUTERUSE_ANTIGRAVITY_ADDRESS</code> /{" "}
              <code>…_CSRF_TOKEN</code>), then reload.
            </div>
          </div>
        )}
        {index?.available && messages.length === 0 && (
          <div className="chat-intro dim">
            <p>Ask about the agent, the session on screen, or anything else.</p>
            {sessionId ? (
              <p>
                {attach
                  ? "Each message carries the part of this session's timeline the chat has not seen yet"
                  : "Turn on “Share session” to include this session's timeline"}
                {attach && screenshot ? " and the latest screenshot." : "."}
              </p>
            ) : (
              <p>Open a session to let the chat see its timeline and screenshots.</p>
            )}
            <p>The model can only answer — it cannot click, type, or change a session.</p>
          </div>
        )}
        {messages.map((m) => (
          <MessageView key={m.id} m={m} />
        ))}
      </div>

      <div className="chat-composer">
        {sessionId && (
          <div className="chat-ctx">
            <label className="check" title="Append the unseen part of this session's timeline to each message">
              <input
                type="checkbox"
                checked={share}
                onChange={(e) => {
                  setShare(e.target.checked);
                  remember(KEYS.share, e.target.checked ? "1" : "0");
                }}
              />
              Share session <code>{sessionId.slice(0, 8)}</code>
            </label>
            {share && (
              <label className="check" title="Also attach the latest screenshot (models that accept images only)">
                <input
                  type="checkbox"
                  checked={screenshot}
                  onChange={(e) => {
                    setScreenshot(e.target.checked);
                    remember(KEYS.shot, e.target.checked ? "1" : "0");
                  }}
                />
                + screenshot
              </label>
            )}
          </div>
        )}
        <textarea
          ref={inputRef}
          className="textarea chat-input"
          rows={rows}
          placeholder={index?.available ? "Message… (Enter to send, Shift+Enter for a new line)" : "Chat unavailable"}
          value={draft}
          disabled={!index?.available}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKey}
        />
        <div className="chat-actions">
          <select
            className="select chat-model"
            value={effectiveModel}
            onChange={(e) => pickModel(e.target.value)}
            disabled={!index?.available || models.length === 0}
            title="Model for the next message (switching keeps the conversation)"
            aria-label="Chat model"
          >
            {models.length === 0 && <option value="">no models</option>}
            {models.map((m) => (
              <option key={m.id} value={m.id} disabled={!m.available} title={m.reason}>
                {m.label}
                {!m.available ? " · quota" : !m.supports_images ? " · text only" : ""}
              </option>
            ))}
          </select>
          {pending ? (
            <button className="btn sm" onClick={() => void stop()} title="Stop generating">
              <Spinner /> Stop
            </button>
          ) : (
            <button className="btn sm primary" onClick={() => void send()} disabled={!canSend}>
              Send
            </button>
          )}
        </div>
      </div>
    </aside>
  );
}

function MessageView({ m }: { m: ChatMessage }) {
  const ctx = m.context;
  return (
    <div className={`msg ${m.role}${m.error ? " failed" : ""}`}>
      {m.role === "user" ? (
        <div className="msg-body user-text">{m.text}</div>
      ) : m.pending ? (
        <div className="msg-body dim">
          <Spinner /> {m.model_label ?? "model"} is thinking…
        </div>
      ) : (
        <>
          {m.text && <Markdown text={m.text} className="msg-body" />}
          {m.error && <div className="msg-error">{m.error === "cancelled" ? "Stopped." : m.error}</div>}
        </>
      )}
      <div className="meta dim">
        {m.role === "user" && ctx && (
          <span title={ctx.note ?? `events ${ctx.after_seq + 1}–${ctx.until_seq} of session ${ctx.session_id}`}>
            session {ctx.session_id.slice(0, 8)} ·{" "}
            {ctx.events === 0 ? "no changes" : `${ctx.events} new ${ctx.events === 1 ? "entry" : "entries"}`}
            {ctx.first ? " · task" : ""}
            {ctx.screenshot ? " · screenshot" : ""}
          </span>
        )}
        {m.role === "assistant" && !m.pending && (
          <>
            <span>{m.model_label ?? m.model ?? ""}</span>
            {m.latency_ms != null && <span>{fmtMs(m.latency_ms)}</span>}
            {m.usage && (
              <span title="input / output tokens">
                {m.usage.input_tokens}↑ {m.usage.output_tokens}↓
              </span>
            )}
            {m.thinking && (
              <details className="msg-thinking">
                <summary>thinking</summary>
                <pre>{truncate(m.thinking, 4000)}</pre>
              </details>
            )}
          </>
        )}
      </div>
    </div>
  );
}
