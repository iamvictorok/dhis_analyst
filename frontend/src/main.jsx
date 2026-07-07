/**
 * Main Preact application — ChatGPT-style layout with sidebar + chat thread.
 *
 * Architecture:
 * - Sidebar: backend-persisted conversation history
 * - Main area: full-width chat thread with inline artefacts
 * - Composer: fixed bottom input with mode selector
 * - All artefact components (Dashboard, ReportEditor, DownloadBar, EvidenceTag)
 *   render inline within the message thread AND remain fully functional/downloadable
 */
import { render } from 'preact';
import { useState, useEffect, useCallback, useRef } from 'preact/hooks';
import '../styles.css';
import { marked } from 'marked';

// Configure marked for GFM and line breaks
marked.use({ gfm: true, breaks: true });

import { getAuth, isAuthenticated, clearAuth } from './auth/standaloneAuth.js';
import { detectDhis2 } from './auth/dhis2Auth.js';
import { readSseStream } from './lib/stream.js';
import { sanitizeHtml } from './lib/sanitize.js';
import Dashboard from './components/Dashboard.jsx';
import ReportEditor from './components/ReportEditor.jsx';
import DownloadBar from './components/DownloadBar.jsx';
import CopyButton from './components/CopyButton.jsx';
import EvidenceTag from './components/EvidenceTag.jsx';
import ClarificationPrompt from './components/ClarificationPrompt.jsx';
import LoginModal from './components/LoginModal.jsx';
import Sidebar from './components/Sidebar.jsx';

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------

function authHeaders() {
  const auth = getAuth();
  const h = { 'Content-Type': 'application/json' };
  if (auth?.token) h['Authorization'] = `Bearer ${auth.token}`;
  return h;
}

// Registered by the App so any API helper can force a return to the login
// screen when the session token has expired (backend JWTs are short-lived).
let onSessionExpired = null;

function handleAuthFailure(status) {
  if (status === 401 && typeof onSessionExpired === 'function') {
    onSessionExpired();
  }
}

async function apiGet(path) {
  const res = await fetch(path, { headers: authHeaders() });
  if (!res.ok) { handleAuthFailure(res.status); throw new Error(`${res.status}`); }
  return res.json();
}

async function apiPost(path, body = {}) {
  const res = await fetch(path, {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify(body),
  });
  if (!res.ok) { handleAuthFailure(res.status); throw new Error(`${res.status}`); }
  return res.json();
}

async function apiDelete(path) {
  const res = await fetch(path, { method: 'DELETE', headers: authHeaders() });
  if (!res.ok) { handleAuthFailure(res.status); throw new Error(`${res.status}`); }
  return res.json();
}

async function apiPatch(path, body) {
  const res = await fetch(path, {
    method: 'PATCH',
    headers: authHeaders(),
    body: JSON.stringify(body),
  });
  if (!res.ok) { handleAuthFailure(res.status); throw new Error(`${res.status}`); }
  return res.json();
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------

function App() {
  const [authed, setAuthed] = useState(isAuthenticated());
  const [isDhis2, setIsDhis2] = useState(false);

  // Conversations
  const [conversations, setConversations] = useState([]);
  const [activeConvId, setActiveConvId] = useState(null);

  // Current conversation state
  const [messages, setMessages] = useState([]);
  const [loading, setLoading] = useState(false);
  const [mode, setMode] = useState('');
  const [clarification, setClarification] = useState(null);

  // Artefacts accumulated during current stream (for the active assistant msg)
  const streamArtefacts = useRef({ charts: [], reportHtml: '', slides: [], evidence: [], exportData: null });

  // Sidebar state
  const [sidebarOpen, setSidebarOpen] = useState(false);

  const threadRef = useRef(null);
  // Aborts the in-flight chat stream when the user navigates away mid-response,
  // so its events can't leak into (and persist onto) another conversation.
  const abortRef = useRef(null);

  useEffect(() => {
    setIsDhis2(detectDhis2());
    if (detectDhis2()) setAuthed(true);
  }, []);

  // Fetch conversations on auth
  useEffect(() => {
    if (authed) {
      apiGet('/api/conversations')
        .then(setConversations)
        .catch(() => setConversations([]));
    }
  }, [authed]);

  // Return to the login screen when any API call reports an expired session,
  // instead of leaving the app "authenticated" while every request 401s.
  useEffect(() => {
    onSessionExpired = () => {
      clearAuth();
      setAuthed(false);
      setConversations([]);
      setActiveConvId(null);
      setMessages([]);
      setClarification(null);
    };
    return () => { onSessionExpired = null; };
  }, []);

  const scrollToBottom = useCallback(() => {
    if (threadRef.current) {
      threadRef.current.scrollTop = threadRef.current.scrollHeight;
    }
  }, []);

  // ---------------------------------------------------------------------------
  // Conversation management
  // ---------------------------------------------------------------------------

  const handleNewChat = useCallback(async () => {
    abortRef.current?.abort();
    try {
      const conv = await apiPost('/api/conversations');
      setConversations(prev => [conv, ...prev]);
      setActiveConvId(conv.id);
      setMessages([]);
      setClarification(null);
      streamArtefacts.current = { charts: [], reportHtml: '', slides: [], evidence: [], exportData: null };
      setSidebarOpen(false);
    } catch (err) {
      console.error('Failed to create conversation:', err);
    }
  }, []);

  const handleSelectConv = useCallback(async (id) => {
    if (id === activeConvId) {
      setSidebarOpen(false);
      return;
    }
    abortRef.current?.abort();
    setActiveConvId(id);
    setClarification(null);
    streamArtefacts.current = { charts: [], reportHtml: '', slides: [], evidence: [], exportData: null };
    setSidebarOpen(false);
    try {
      const msgs = await apiGet(`/api/conversations/${id}/messages`);
      setMessages(msgs);
      // Scroll after render
      setTimeout(scrollToBottom, 50);
    } catch {
      setMessages([]);
    }
  }, [activeConvId, scrollToBottom]);

  const handleDeleteConv = useCallback(async (id) => {
    if (id === activeConvId) abortRef.current?.abort();
    try {
      await apiDelete(`/api/conversations/${id}`);
      setConversations(prev => prev.filter(c => c.id !== id));
      if (activeConvId === id) {
        setActiveConvId(null);
        setMessages([]);
      }
    } catch (err) {
      console.error('Failed to delete conversation:', err);
    }
  }, [activeConvId]);

  // ---------------------------------------------------------------------------
  // Send message
  // ---------------------------------------------------------------------------

  const handleSend = useCallback(async (text) => {
    if (!text.trim() || loading) return;
    setClarification(null);
    setLoading(true);

    // Abort any previous stream and track this one so navigation can cancel it.
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    // Reset stream artefacts
    streamArtefacts.current = { charts: [], reportHtml: '', slides: [], evidence: [], exportData: null };

    // Ensure we have an active conversation
    let convId = activeConvId;
    if (!convId) {
      try {
        const conv = await apiPost('/api/conversations');
        convId = conv.id;
        setConversations(prev => [conv, ...prev]);
        setActiveConvId(convId);
      } catch (err) {
        console.error('Failed to create conversation:', err);
        setLoading(false);
        return;
      }
    }

    // Add user + placeholder assistant message to UI
    const userMsg = { role: 'user', content: text, artefacts: null };
    const assistantMsg = { role: 'assistant', content: '', artefacts: null };
    setMessages(prev => [...prev, userMsg, assistantMsg]);

    // Persist user message to backend
    try {
      await apiPost(`/api/conversations/${convId}/messages`, {
        role: 'user',
        content: text,
      });
    } catch { /* non-critical */ }

    // Auto-title from first message
    const isFirst = messages.length === 0;
    if (isFirst) {
      const title = text.length > 40 ? text.slice(0, 40) + '…' : text;
      apiPatch(`/api/conversations/${convId}`, { title }).catch(() => {});
      setConversations(prev =>
        prev.map(c => c.id === convId ? { ...c, title } : c)
      );
    }

    try {
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: authHeaders(),
        signal: controller.signal,
        body: JSON.stringify({
          message: text,
          conversation_id: convId,
          output_mode: mode || null,
          allow_web: true,
        }),
      });

      if (res.status === 401) {
        handleAuthFailure(401);
        throw new Error('Your session expired. Please log in again.');
      }
      if (!res.ok || !res.body) throw new Error(`Request failed: ${res.status}`);

      await readSseStream(res, (type, data) => {
        switch (type) {
          case 'token':
            setMessages(prev => {
              const updated = [...prev];
              const last = updated[updated.length - 1];
              if (last?.role === 'assistant') {
                updated[updated.length - 1] = { ...last, content: (last.content || '') + (data.text || '') };
              }
              return updated;
            });
            scrollToBottom();
            break;
          case 'chart_config':
            streamArtefacts.current.charts = [
              ...streamArtefacts.current.charts.filter(c => c.id !== data.id),
              data,
            ];
            // Update last assistant message artefacts in UI
            setMessages(prev => {
              const updated = [...prev];
              const last = updated[updated.length - 1];
              if (last?.role === 'assistant') {
                updated[updated.length - 1] = {
                  ...last,
                  artefacts: { ...streamArtefacts.current },
                };
              }
              return updated;
            });
            break;
          case 'report_html':
            streamArtefacts.current.reportHtml = data.html || '';
            setMessages(prev => {
              const updated = [...prev];
              const last = updated[updated.length - 1];
              if (last?.role === 'assistant') {
                updated[updated.length - 1] = {
                  ...last,
                  artefacts: { ...streamArtefacts.current },
                };
              }
              return updated;
            });
            break;
          case 'slide_manifest':
            streamArtefacts.current.slides = Array.isArray(data) ? data : [];
            setMessages(prev => {
              const updated = [...prev];
              const last = updated[updated.length - 1];
              if (last?.role === 'assistant') {
                updated[updated.length - 1] = {
                  ...last,
                  artefacts: { ...streamArtefacts.current },
                };
              }
              return updated;
            });
            break;
          case 'evidence':
            streamArtefacts.current.evidence = Array.isArray(data) ? data : [];
            setMessages(prev => {
              const updated = [...prev];
              const last = updated[updated.length - 1];
              if (last?.role === 'assistant') {
                updated[updated.length - 1] = {
                  ...last,
                  artefacts: { ...streamArtefacts.current },
                };
              }
              return updated;
            });
            break;
          case 'data_ready':
            streamArtefacts.current.exportData = data.data || data;
            setMessages(prev => {
              const updated = [...prev];
              const last = updated[updated.length - 1];
              if (last?.role === 'assistant') {
                updated[updated.length - 1] = {
                  ...last,
                  artefacts: { ...streamArtefacts.current },
                };
              }
              return updated;
            });
            break;
          case 'clarification': {
            const question = data.question || 'Could you clarify your request?';
            setClarification(question);
            // Surface the question as the assistant turn so the bubble isn't
            // empty and the question is persisted as context for the reply.
            setMessages(prev => {
              const updated = [...prev];
              const last = updated[updated.length - 1];
              if (last?.role === 'assistant') {
                updated[updated.length - 1] = { ...last, content: question };
              }
              return updated;
            });
            break;
          }
          case 'error':
            setMessages(prev => {
              const updated = [...prev];
              const last = updated[updated.length - 1];
              if (last?.role === 'assistant') {
                updated[updated.length - 1] = { ...last, content: data.user_message || 'An error occurred.' };
              }
              return updated;
            });
            break;
        }
      });
    } catch (err) {
      // A deliberate abort (conversation switch) is not an error to surface.
      if (err.name !== 'AbortError' && !controller.signal.aborted) {
        setMessages(prev => {
          const updated = [...prev];
          const last = updated[updated.length - 1];
          if (last?.role === 'assistant') {
            updated[updated.length - 1] = { ...last, content: err.message };
          }
          return updated;
        });
      }
    }

    // Persist + reorder only when this stream wasn't cancelled mid-flight. On an
    // abort the user has already navigated away; touching state/history here
    // would corrupt (and cross-persist into) the wrong conversation.
    if (!controller.signal.aborted) {
      // Persist assistant message to backend (with artefacts). Skip a completely
      // empty turn — persisting one poisons the history the intent classifier
      // replays on the next question.
      try {
        setMessages(prev => {
          const last = prev[prev.length - 1];
          const a = streamArtefacts.current;
          const hasArtefacts = a.charts.length || a.reportHtml || a.slides.length || a.evidence.length || a.exportData;
          if (last?.role === 'assistant' && (last.content || hasArtefacts)) {
            apiPost(`/api/conversations/${convId}/messages`, {
              role: 'assistant',
              content: last.content || '',
              artefacts: a,
            }).catch(() => {});
          }
          return prev;
        });
      } catch { /* non-critical */ }

      // Move conversation to top of list
      setConversations(prev => {
        const idx = prev.findIndex(c => c.id === convId);
        if (idx > 0) {
          const conv = prev[idx];
          return [{ ...conv, updated_at: new Date().toISOString() }, ...prev.filter(c => c.id !== convId)];
        }
        return prev;
      });
    }

    if (abortRef.current === controller) {
      abortRef.current = null;
    }
    setLoading(false);
  }, [loading, mode, activeConvId, messages.length, scrollToBottom]);

  // ---------------------------------------------------------------------------
  // Auth
  // ---------------------------------------------------------------------------

  const handleLogin = useCallback(() => setAuthed(true), []);
  const handleLogout = useCallback(() => {
    clearAuth();
    setAuthed(false);
    setConversations([]);
    setActiveConvId(null);
    setMessages([]);
  }, []);

  if (!authed) {
    return <LoginModal onLogin={handleLogin} />;
  }


  return (
    <div class="layout">
      <Sidebar
        conversations={conversations}
        activeId={activeConvId}
        onSelect={handleSelectConv}
        onNew={handleNewChat}
        onDelete={handleDeleteConv}
        isOpen={sidebarOpen}
        onToggle={() => setSidebarOpen(v => !v)}
      />

      <div class="main-area">
        <header class="main-header">
          <div class="main-header-left">
            <button
              class="sidebar-toggle-btn"
              onClick={() => setSidebarOpen(v => !v)}
              title="Toggle sidebar"
              aria-label="Toggle sidebar"
            >
              ☰
            </button>
            <h1 style="font-size:0.95rem;font-weight:700;background:linear-gradient(135deg,var(--accent),var(--success));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text">
              DHIS2 AI Analyst
            </h1>
          </div>
          <div class="main-header-right">
            <button class="btn-sm" onClick={handleLogout}>Logout</button>
          </div>
        </header>

        <div class="thread" ref={threadRef} aria-live="polite">
          <div class="thread-inner">
            {messages.length === 0 && !activeConvId && (
              <div class="thread-welcome">
                <p class="thread-welcome-title">What can I help you with?</p>
                <p class="thread-welcome-sub">
                  Ask a public health question about your DHIS2 data — trends, comparisons, dashboards, reports, and more.
                </p>
              </div>
            )}

            {messages.length === 0 && activeConvId && (
              <div class="thread-welcome">
                <p class="thread-welcome-title">💡 Ask a public health question</p>
                <p class="thread-welcome-sub">
                  Try: "Show me malaria trends in Kaduna over the last quarter"
                </p>
              </div>
            )}

            {messages.map((msg, i) => (
              <MessageBlock
                key={i}
                msg={msg}
                isLast={i === messages.length - 1}
                isLoading={loading && i === messages.length - 1 && msg.role === 'assistant'}
              />
            ))}
          </div>
        </div>

        {clarification && (
          <div style="display:flex;justify-content:center;padding:0 16px">
            <div style="max-width:var(--thread-max);width:100%">
              <ClarificationPrompt question={clarification} />
            </div>
          </div>
        )}

        <Composer
          loading={loading}
          mode={mode}
          onModeChange={setMode}
          onSend={handleSend}
        />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// MessageBlock — renders a single message with inline artefacts
// ---------------------------------------------------------------------------

function MessageBlock({ msg, isLast, isLoading }) {
  const isUser = msg.role === 'user';
  const art = msg.artefacts;
  const hasCharts = art?.charts?.length > 0;
  const hasReport = !!art?.reportHtml;
  const hasEvidence = art?.evidence?.length > 0;
  const hasSlides = art?.slides?.length > 0;
  const hasExport = !!art?.exportData;
  const [sourcesOpen, setSourcesOpen] = useState(false);

  return (
    <div class={`message ${isUser ? 'message-user' : 'message-assistant'}`}>
      {!isUser && <span class="message-label">Assistant</span>}
      {isUser && <span class="message-label">You</span>}

      <div class="message-bubble">
        {msg.content ? (
          <div dangerouslySetInnerHTML={{ __html: sanitizeHtml(marked.parse(msg.content)) }} />
        ) : (isLoading ? (
          <span class="typing-indicator">
            <span class="typing-dot" />
            <span class="typing-dot" />
            <span class="typing-dot" />
          </span>
        ) : '')}
        {!isUser && msg.content && (
          <div class="message-actions">
            <CopyButton text={msg.content} label="Copy" />
          </div>
        )}
      </div>

      {/* Inline artefacts — only for assistant messages */}
      {!isUser && (hasCharts || hasReport || hasEvidence || hasSlides || hasExport) && (
        <div class="message-artefacts">
          {hasCharts && (
            <div class="artefact-section">
              <div class="artefact-section-head" style="display: flex; align-items: center; justify-content: space-between; width: 100%;">
                <div style="display: flex; align-items: center; gap: 8px;">
                  <h3>📊 Dashboard</h3>
                  <span class="badge">{art.charts.length} chart{art.charts.length !== 1 ? 's' : ''}</span>
                </div>
                <div class="artefact-actions">
                  <CopyButton
                    text={JSON.stringify(art.charts.map(c => ({ id: c.id, title: c.layout?.title, series: c.series })), null, 2)}
                    label="Copy data"
                  />
                </div>
              </div>
              <Dashboard charts={art.charts} />
            </div>
          )}

          {hasReport && (
            <div class="artefact-section">
              <div class="artefact-section-head" style="display: flex; align-items: center; justify-content: space-between; width: 100%;">
                <div style="display: flex; align-items: center; gap: 8px;">
                  <h3>📄 Report</h3>
                  <span class="badge">Editable</span>
                </div>
                <div class="artefact-actions">
                  <CopyButton text={art.reportHtml} label="Copy HTML" />
                  <DownloadBar reportHtml={art.reportHtml} allowedFormats={['DOCX', 'PDF']} />
                </div>
              </div>
              <ReportEditor html={art.reportHtml} onChange={() => {}} />
            </div>
          )}

          {hasSlides && (
            <div class="artefact-section">
              <div class="artefact-section-head" style="display: flex; align-items: center; justify-content: space-between; width: 100%;">
                <div style="display: flex; align-items: center; gap: 8px;">
                  <h3>🎴 Presentation Slides</h3>
                  <span class="badge">{art.slides.length} slides</span>
                </div>
                <div class="artefact-actions">
                  <CopyButton text={JSON.stringify(art.slides, null, 2)} label="Copy slides" />
                  <DownloadBar slides={art.slides} allowedFormats={['PPTX']} />
                </div>
              </div>
              <div class="slides-preview-wrapper" style="overflow-x: auto; display: flex; gap: 12px; padding: 8px 0; margin-top: 8px;">
                {art.slides.map((slide, sIdx) => (
                  <div key={sIdx} style="flex: 0 0 220px; border: 1px solid var(--border-color); border-radius: 6px; padding: 12px; background: var(--bg-glass); backdrop-filter: blur(10px); box-shadow: var(--shadow-sm);">
                    <h4 style="margin: 0 0 8px 0; font-size: 0.8rem; font-weight: 600; color: var(--accent); border-bottom: 1px solid var(--border-color); padding-bottom: 4px;">{slide.title || `Slide ${sIdx + 1}`}</h4>
                    <ul style="margin: 0; padding-left: 14px; font-size: 0.72rem; color: var(--text-secondary); line-height: 1.4;">
                      {(slide.bullets || []).slice(0, 4).map((bullet, bIdx) => (
                        <li key={bIdx}>{bullet}</li>
                      ))}
                    </ul>
                  </div>
                ))}
              </div>
            </div>
          )}

          {hasExport && (
            <div class="artefact-section">
              <div class="artefact-section-head" style="display: flex; align-items: center; justify-content: space-between; width: 100%;">
                <div style="display: flex; align-items: center; gap: 8px;">
                  <h3>📋 Export Data</h3>
                  <span class="badge">{(art.exportData.rows || []).length} rows</span>
                </div>
                <div class="artefact-actions">
                  <CopyButton
                    text={[
                      (art.exportData.headers || []).join('\t'),
                      ...(art.exportData.rows || []).map(r => r.join('\t')),
                    ].join('\n')}
                    label="Copy table"
                  />
                  <DownloadBar exportData={art.exportData} allowedFormats={['XLSX', 'CSV']} />
                </div>
              </div>
              <div class="export-preview-table-wrapper" style="overflow-x: auto; max-height: 250px; border: 1px solid var(--border-color); border-radius: 6px; background: var(--bg-primary); margin-top: 8px;">
                <table class="export-preview-table" style="width: 100%; border-collapse: collapse; font-size: 0.85rem;">
                  <thead>
                    <tr style="background: var(--bg-secondary); border-bottom: 2px solid var(--border-color);">
                      {(art.exportData.headers || []).map((h, idx) => (
                        <th key={idx} style="padding: 8px 12px; text-align: left; font-weight: 600; border-right: 1px solid var(--border-color);">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {(art.exportData.rows || []).slice(0, 5).map((row, rIdx) => (
                      <tr key={rIdx} style="border-bottom: 1px solid var(--border-color); background: rIdx % 2 === 0 ? 'transparent' : 'var(--bg-secondary)';">
                        {row.map((cell, cIdx) => (
                          <td key={cIdx} style="padding: 8px 12px; border-right: 1px solid var(--border-color);">{cell}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {(art.exportData.rows || []).length > 5 && (
                <p style="font-size: 0.75rem; color: var(--text-muted); margin-top: 8px; font-style: italic;">
                  Showing first 5 rows. Use the download buttons above to get the full dataset.
                </p>
              )}
            </div>
          )}

          {hasEvidence && (
            <div class="sources-section">
              <button
                class="sources-toggle"
                onClick={() => setSourcesOpen(v => !v)}
              >
                <span class={`sources-toggle-icon ${sourcesOpen ? 'sources-toggle-icon--open' : ''}`}>▶</span>
                {art.evidence.length} source{art.evidence.length !== 1 ? 's' : ''}
              </button>
              {sourcesOpen && (
                <div class="sources-list">
                  {art.evidence.map((item, j) => (
                    <EvidenceTag key={j} item={item} />
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Composer — fixed-bottom input area
// ---------------------------------------------------------------------------

function Composer({ loading, mode, onModeChange, onSend }) {
  const [text, setText] = useState('');
  const textareaRef = useRef(null);

  const handleSubmit = (e) => {
    e.preventDefault();
    // Do not clear the input while a response is streaming — handleSend rejects
    // the send when loading, so clearing here would silently discard the text.
    if (loading || !text.trim()) return;
    onSend(text);
    setText('');
    // Reset textarea height
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e);
    }
  };

  const handleInput = (e) => {
    setText(e.target.value);
    // Auto-grow textarea
    const ta = e.target;
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, 160) + 'px';
  };

  return (
    <div class="composer-wrapper">
      <form class="composer" onSubmit={handleSubmit}>
        <select
          id="mode-select"
          value={mode}
          onChange={(e) => onModeChange(e.target.value)}
          title="Output mode"
        >
          <option value="">Auto</option>
          <option value="conversational">Answer</option>
          <option value="dashboard">Dashboard</option>
          <option value="report">Report</option>
          <option value="presentation">Slides</option>
          <option value="export">Export</option>
        </select>
        <textarea
          ref={textareaRef}
          id="chat-input"
          rows="1"
          value={text}
          onInput={handleInput}
          onKeyDown={handleKeyDown}
          placeholder="Ask a public health question..."
        />
        <button
          id="send-btn"
          type="submit"
          class="composer-send-btn"
          disabled={loading || !text.trim()}
          title={loading ? 'Working…' : 'Send'}
        >
          {loading ? '⏳' : '↑'}
        </button>
      </form>
    </div>
  );
}

render(<App />, document.getElementById('app'));
