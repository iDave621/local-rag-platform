import React, { useCallback, useEffect, useRef, useState } from 'react';
import { api, dateLabel, isPending, jobLabel, mergeJobs, sourceLabel } from './api.js';
import { readStored, rememberJobs, writeStored } from './storage.js';
import AdminPanel from './AdminPanel.jsx';

function Login({ onLogin, notice }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  async function submit(event) {
    event.preventDefault();
    if (busy) return;
    setError('');
    setBusy(true);
    try {
      const user = await api('/api/auth/login', {
        method: 'POST', body: JSON.stringify({ username: username.trim(), password }),
      });
      setPassword('');
      onLogin(user);
    } catch (failure) {
      setError(failure.status === 401 ? 'שם המשתמש או הסיסמה שגויים.' : failure.message);
    } finally { setBusy(false); }
  }

  return <main className="login-page">
    <form className="card login-card" onSubmit={submit}>
      <div className="eyebrow">LOCAL RAG</div>
      <h1>כניסה למערכת</h1>
      <p className="muted">תשובות מתוך המסמכים של הארגון, בשרת המקומי.</p>
      {notice && <p className="notice" role="status">{notice}</p>}
      <label htmlFor="username">שם משתמש</label>
      <input id="username" autoComplete="username" required autoFocus maxLength={80}
        value={username} onChange={event => setUsername(event.target.value)} disabled={busy} />
      <label htmlFor="password">סיסמה</label>
      <input id="password" type="password" autoComplete="current-password" required maxLength={256}
        value={password} onChange={event => setPassword(event.target.value)} disabled={busy} />
      <button className="full-width" disabled={busy || !username.trim() || !password}>
        {busy ? 'מתחבר…' : 'כניסה'}
      </button>
      {error && <p className="error" role="alert">{error}</p>}
    </form>
  </main>;
}

function Sources({ sources }) {
  if (!Array.isArray(sources) || !sources.length) return null;
  return <details className="sources">
    <summary>מקורות לתשובה ({sources.length})</summary>
    <ul>{sources.map((source, index) => <li key={`${source.id || index}-${source.source || ''}`}>
      <span className="citation" dir="ltr">[{source.id || `S${index + 1}`}]</span>{' '}
      <bdi>{sourceLabel(source)}</bdi>
      {source.page != null && source.page !== '' && <span> · עמוד {source.page}</span>}
      {source.version && <span> · גרסה <bdi>{source.version}</bdi></span>}
    </li>)}</ul>
  </details>;
}

export default function App() {
  const [user, setUser] = useState(null);
  const [checkingAuth, setCheckingAuth] = useState(true);
  const [authNotice, setAuthNotice] = useState('');
  const [sessions, setSessions] = useState([]);
  const [sessionId, setSessionId] = useState(null);
  const [messages, setMessages] = useState([]);
  const [jobs, setJobs] = useState([]);
  const [jobsOwner, setJobsOwner] = useState(null);
  const [workspaceBusy, setWorkspaceBusy] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);
  const [text, setText] = useState('');
  const [error, setError] = useState('');
  const [connectionNotice, setConnectionNotice] = useState('');
  const [offline, setOffline] = useState(!navigator.onLine);
  const [view, setView] = useState('chat');
  const selectedRef = useRef(null);
  const selectionVersion = useRef(0);
  const userRef = useRef(null);
  const draftRequest = useRef(null);
  const messageEnd = useRef(null);
  selectedRef.current = sessionId;
  userRef.current = user;

  const request = useCallback(async (path, options) => {
    const account = userRef.current?.id;
    try { return await api(path, options); }
    catch (failure) {
      if (failure.status === 401 && userRef.current?.id === account) {
        selectionVersion.current += 1;
        setUser(null);
        setSessionId(null);
        setMessages([]);
        setJobs([]);
        setJobsOwner(null);
        setAuthNotice('הכניסה למערכת פגה. לאחר התחברות מחדש אפשר להמשיך באותה שיחה.');
      }
      throw failure;
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    api('/api/auth/me', { signal: controller.signal })
      .then(setUser)
      .catch(failure => {
        if (failure.name !== 'AbortError' && failure.status !== 401) setAuthNotice(failure.message);
      })
      .finally(() => { if (!controller.signal.aborted) setCheckingAuth(false); });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const onOnline = () => setOffline(false);
    const onOffline = () => setOffline(true);
    window.addEventListener('online', onOnline);
    window.addEventListener('offline', onOffline);
    return () => {
      window.removeEventListener('online', onOnline);
      window.removeEventListener('offline', onOffline);
    };
  }, []);

  useEffect(() => {
    if (!user) return;
    const controller = new AbortController();
    const version = ++selectionVersion.current;
    const storedJobs = readStored(user.id, 'jobs', []);
    setJobs(Array.isArray(storedJobs) ? storedJobs.filter(job => job?.job_id && job?.kind) : []);
    setJobsOwner(user.id);
    setWorkspaceBusy(true);
    setError('');
    setText('');
    draftRequest.current = null;
    setView('chat');
    setMessages([]);
    setSessionId(null);
    async function initialize() {
      try {
        const list = await request('/api/sessions', { signal: controller.signal });
        if (controller.signal.aborted) return;
        setSessions(list);
        const saved = readStored(user.id, 'session', null);
        const previous = list.find(item => item.session_id === saved) || list[0];
        const current = previous
          ? await request(`/api/sessions/${previous.session_id}/resume`, { method: 'POST', signal: controller.signal })
          : await request('/api/sessions', { method: 'POST', signal: controller.signal });
        const serverJobs = await request('/api/jobs', { signal: controller.signal });
        const history = await request(`/api/sessions/${current.session_id}/history`, { signal: controller.signal });
        if (controller.signal.aborted || version !== selectionVersion.current) return;
        setSessionId(current.session_id);
        setMessages(history);
        writeStored(user.id, 'session', current.session_id);
        if (!previous) setSessions([{ ...current, title: 'שיחה חדשה', created_at: new Date().toISOString() }]);
        setJobs(existing => mergeJobs(existing, serverJobs));
      } catch (failure) {
        if (failure.name !== 'AbortError') setError(failure.message);
      } finally {
        if (!controller.signal.aborted && version === selectionVersion.current) setWorkspaceBusy(false);
      }
    }
    initialize();
    return () => controller.abort();
  }, [user?.id, request]);

  useEffect(() => {
    if (user && jobsOwner === user.id) rememberJobs(user.id, jobs);
  }, [user?.id, jobsOwner, jobs]);

  const refreshHistory = useCallback(async (sid, signal) => {
    const account = userRef.current?.id;
    const history = await request(`/api/sessions/${sid}/history`, { signal });
    if (selectedRef.current === sid && userRef.current?.id === account) setMessages(history);
  }, [request]);

  const activeKey = jobs.filter(isPending).map(job => job.job_id).sort().join(',');
  useEffect(() => {
    if (!user || !activeKey) return;
    const controller = new AbortController();
    let timer;
    const pending = jobs.filter(isPending);
    async function poll() {
      const results = await Promise.allSettled(pending.map(job =>
        request(`/api/jobs/${job.job_id}`, { signal: controller.signal })));
      if (controller.signal.aborted) return;
      const updates = new Map();
      const completedSessions = new Set();
      let disconnected = false;
      results.forEach((result, index) => {
        const original = pending[index];
        if (result.status === 'fulfilled') {
          const latest = mergeJobs([original], [result.value])[0];
          updates.set(original.job_id, latest);
          if (!isPending(latest) && latest.kind === 'chat') completedSessions.add(latest.session_id);
        } else if (result.reason.status === 404) {
          updates.set(original.job_id, { ...original, status: 'failed', error: 'לא נמצא מידע על הבקשה בשרת.' });
        } else if (result.reason.status !== 401 && result.reason.name !== 'AbortError') {
          disconnected = true;
        }
      });
      setConnectionNotice(disconnected ? 'עדכון מצב הבקשות נעצר זמנית. המערכת תנסה להתחבר שוב; אפשר לרענן בלי לאבד בקשות שנשלחו.' : '');
      // Refresh persisted messages before removing the last pending indicator.
      for (const sid of completedSessions) {
        if (sid === selectedRef.current) {
          try { await refreshHistory(sid, controller.signal); }
          catch (failure) {
            if (failure.name !== 'AbortError') {
              setError(failure.message);
              disconnected = true;
              // Keep polling until the saved answer can also be displayed.
              pending.filter(job => job.session_id === sid).forEach(job => updates.delete(job.job_id));
            }
          }
        }
      }
      if (controller.signal.aborted) return;
      setJobs(current => current.map(job => updates.get(job.job_id) || job));
      if (completedSessions.size) {
        const account = user.id;
        request('/api/sessions').then(list => {
          if (userRef.current?.id === account) setSessions(list);
        }).catch(() => {});
      }
      timer = window.setTimeout(poll, disconnected ? 5000 : 2000);
    }
    poll();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [user?.id, activeKey, request, refreshHistory]);

  useEffect(() => {
    messageEnd.current?.scrollIntoView({ block: 'nearest' });
  }, [messages.length, activeKey, view]);

  async function chooseSession(sid) {
    if (workspaceBusy || submitting) return;
    const account = user.id;
    const version = ++selectionVersion.current;
    setWorkspaceBusy(true);
    setError('');
    setText('');
    setView('chat');
    try {
      const current = sid
        ? await request(`/api/sessions/${sid}/resume`, { method: 'POST' })
        : await request('/api/sessions', { method: 'POST' });
      const serverJobs = await request(`/api/jobs?session_id=${encodeURIComponent(current.session_id)}`);
      const history = await request(`/api/sessions/${current.session_id}/history`);
      if (version !== selectionVersion.current || userRef.current?.id !== account) return;
      setSessionId(current.session_id);
      setMessages(history);
      writeStored(account, 'session', current.session_id);
      if (!sid) setSessions(list => [{ ...current, title: 'שיחה חדשה', created_at: new Date().toISOString() }, ...list]);
      setJobs(existing => mergeJobs(existing, serverJobs));
    } catch (failure) {
      if (version === selectionVersion.current && userRef.current?.id === account) setError(failure.message);
    }
    finally { if (version === selectionVersion.current) setWorkspaceBusy(false); }
  }

  const currentJobs = jobs.filter(job => job.kind === 'chat' && job.session_id === sessionId);
  const waiting = currentJobs.some(isPending);
  const maxMessageChars = user?.max_message_chars || (user?.role === 'admin' ? 12000 : 4000);

  async function send(event) {
    event.preventDefault();
    const question = text.trim();
    if (!question || !sessionId || submitting || waiting || workspaceBusy) return;
    const sid = sessionId;
    const account = user.id;
    setSubmitting(true);
    setError('');
    try {
      if (draftRequest.current?.session_id !== sid || draftRequest.current?.message !== question) {
        draftRequest.current = { session_id: sid, message: question, request_id: crypto.randomUUID() };
      }
      const options = { method: 'POST', body: JSON.stringify(draftRequest.current) };
      let accepted;
      try { accepted = await request('/api/chat', options); }
      catch (failure) {
        if (failure.status !== 440) throw failure;
        await request(`/api/sessions/${sid}/resume`, { method: 'POST' });
        accepted = await request('/api/chat', options);
      }
      if (userRef.current?.id !== account) return;
      setText('');
      draftRequest.current = null;
      setJobs(current => [...current, {
        job_id: accepted.job_id, session_id: accepted.session_id || sid,
        kind: 'chat', status: 'queued', created_at: new Date().toISOString(),
      }]);
      setMessages(current => [...current, { id: `pending-${accepted.job_id}`, role: 'user', content: question }]);
      refreshHistory(sid).catch(failure => setError(failure.message));
    } catch (failure) {
      if (userRef.current?.id !== account) return;
      if (failure.status === 409 && failure.detail?.job_id) {
        setJobs(current => mergeJobs(current, [{
          job_id: failure.detail.job_id, session_id: sid, kind: 'chat', status: 'queued',
        }]));
        setError('לשיחה הזאת כבר נשלחה בקשה. המערכת תציג את התשובה לאחר העיבוד.');
      } else {
        setError(failure.message);
        if (failure.status === 0) {
          request(`/api/jobs?session_id=${encodeURIComponent(sid)}`).then(serverJobs => {
            if (userRef.current?.id === account) setJobs(current => mergeJobs(current, serverJobs));
          }).catch(() => {});
          refreshHistory(sid).catch(() => {});
        }
      }
    } finally { setSubmitting(false); }
  }

  async function signOut() {
    if (loggingOut) return;
    setLoggingOut(true);
    setError('');
    try {
      await request('/api/auth/logout', { method: 'POST' });
      selectionVersion.current += 1;
      setUser(null);
      setSessionId(null);
      setMessages([]);
      setJobs([]);
      setJobsOwner(null);
      setAuthNotice('');
    } catch (failure) { setError(failure.message); }
    finally { setLoggingOut(false); }
  }

  function onLogin(account) {
    setAuthNotice('');
    setConnectionNotice('');
    setUser(account);
  }

  function addJob(job, kind, name) {
    if (userRef.current?.id !== user?.id) return;
    setJobs(current => [...current, {
      ...job, kind, name, status: 'queued', created_at: new Date().toISOString(),
    }]);
  }

  if (checkingAuth) return <main className="login-page"><p role="status">מתחבר לשרת המקומי…</p></main>;
  if (!user) return <Login onLogin={onLogin} notice={authNotice} />;

  return <main className="app-shell">
    <header className="app-header">
      <div><div className="eyebrow">LOCAL RAG</div><h1>מרכז הידע המקומי</h1></div>
      <div className="account"><span><bdi>{user.username}</bdi> · {user.role === 'admin' ? 'מנהל' : 'טכנאי'}</span>
        <button className="secondary" type="button" onClick={signOut} disabled={loggingOut}>
          {loggingOut ? 'מתנתק…' : 'יציאה'}
        </button>
      </div>
    </header>
    {(offline || connectionNotice) && <p className="notice" role="status">
      {offline ? 'אין חיבור לרשת המקומית. בקשות שכבר נשלחו נשמרות בשרת.' : connectionNotice}
    </p>}
    {error && <div className="error-banner" role="alert"><span>{error}</span>
      <button className="text-button" type="button" onClick={() => setError('')} aria-label="סגירת הודעת השגיאה">סגירה</button>
    </div>}
    <div className="workspace">
      <aside className="card sidebar" aria-label="ניווט ושיחות">
        <button className="full-width" type="button" onClick={() => chooseSession(null)} disabled={workspaceBusy || submitting}>שיחה חדשה</button>
        {user.role === 'admin' && <nav className="view-tabs" aria-label="אזורי המערכת">
          <button className={view === 'chat' ? 'selected' : 'secondary'} type="button" onClick={() => setView('chat')}>שיחות</button>
          <button className={view === 'admin' ? 'selected' : 'secondary'} type="button" onClick={() => setView('admin')}>ניהול</button>
        </nav>}
        <h2 className="small-heading">השיחות שלך</h2>
        <div className="session-list">
          {sessions.map(item => <button type="button" key={item.session_id}
            className={`session-button ${item.session_id === sessionId ? 'selected' : ''}`}
            disabled={workspaceBusy || submitting} onClick={() => chooseSession(item.session_id)}
            aria-current={item.session_id === sessionId ? 'true' : undefined}>
            <span>{item.title === 'New conversation' ? 'שיחה חדשה' : item.title || 'שיחה ללא כותרת'}</span>
            <small>{dateLabel(item.last_activity || item.created_at)}</small>
            {jobs.some(job => job.session_id === item.session_id && isPending(job)) && <small className="pending-label">תשובה בעיבוד</small>}
          </button>)}
          {!sessions.length && !workspaceBusy && <p className="muted">אין עדיין שיחות.</p>}
        </div>
        <p className="sidebar-note">השיחה והבקשות נשמרות בשרת. אפשר לחזור אליהן לאחר רענון.</p>
      </aside>
      {view === 'admin' && user.role === 'admin'
        ? <AdminPanel request={request} jobs={jobs} addJob={addJob} />
        : <section className="card chat" aria-label="שיחה עם מרכז הידע">
          <div className="chat-heading"><h2>שאל את המסמכים</h2><p className="muted">תאר את השאלה או התקלה. המקורות יופיעו לצד התשובה.</p></div>
          <div className="messages" aria-live="polite" aria-busy={workspaceBusy || waiting}>
            {workspaceBusy ? <p className="muted">טוען שיחה…</p> : <>
              {!messages.length && <div className="empty-state"><h3>במה אפשר לעזור?</h3><p>אפשר לשאול על נהלים, הגדרות ופתרון תקלות מתוך מאגר המסמכים המקומי.</p></div>}
              {messages.map((message, index) => <article className={`message ${message.role}`} key={message.id || `${message.role}-${index}`}>
                <div className="message-role">{message.role === 'user' ? 'אתה' : 'מרכז הידע'}</div>
                <div className="message-content" dir="auto">{message.content}</div>
                <Sources sources={message.sources} />
              </article>)}
              {currentJobs.filter(isPending).map(job => <div className="message assistant processing" key={job.job_id} role="status">
                <span className="status-dot" />{jobLabel(job)} · מכין תשובה מתוך המסמכים…
                <small>אפשר לעבור לשיחה אחרת או לרענן; התשובה תופיע כשהעיבוד יסתיים.</small>
              </div>)}
              {currentJobs.filter(job => job.status === 'failed').slice(-1).map(job => <div className="error-banner" key={job.job_id} role="alert">
                <span>הכנת התשובה נכשלה: {job.error || 'שגיאה בעיבוד הבקשה.'}</span>
              </div>)}
            </>}
            <div ref={messageEnd} />
          </div>
          <form className="composer" onSubmit={send}>
            <label htmlFor="question" className="visually-hidden">השאלה שלך</label>
            <textarea id="question" value={text} onChange={event => setText(event.target.value)}
              placeholder="כתוב את השאלה שלך…" maxLength={maxMessageChars} rows={3}
              disabled={workspaceBusy || submitting || !sessionId} />
            <div className="composer-actions"><small className="muted">{text.length.toLocaleString('he-IL')} / {maxMessageChars.toLocaleString('he-IL')}</small>
              <button disabled={!text.trim() || !sessionId || workspaceBusy || submitting || waiting}>
                {submitting ? 'שולח…' : waiting ? 'ממתין לתשובה…' : 'שליחה'}
              </button>
            </div>
          </form>
        </section>}
    </div>
  </main>;
}
