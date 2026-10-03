import React, { useCallback, useEffect, useRef, useState } from 'react';
import { dateLabel, isPending, jobLabel } from './api.js';

const documentStatus = {
  active: 'פעיל', inactive: 'לא פעיל', superseded: 'הוחלף',
  staging: 'בעיבוד', pending: 'בעיבוד', deleting: 'בהסרה', deleted: 'הוסר', failed: 'נכשל',
};

export default function AdminPanel({ request, jobs, addJob }) {
  const [tab, setTab] = useState('documents');
  const [documents, setDocuments] = useState([]);
  const [users, setUsers] = useState([]);
  const [file, setFile] = useState(null);
  const [uploading, setUploading] = useState(false);
  const [loadingDocuments, setLoadingDocuments] = useState(false);
  const [deleting, setDeleting] = useState(null);
  const [savingParameter, setSavingParameter] = useState(false);
  const [savingUser, setSavingUser] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [parameter, setParameter] = useState({ key: '', value: '', description: '' });
  const [newUser, setNewUser] = useState({ username: '', password: '', role: 'technician' });
  const inputFile = useRef(null);
  const mounted = useRef(true);
  const documentJobs = jobs.filter(job => job.kind !== 'chat');
  const completedKey = documentJobs.filter(job => !isPending(job)).map(job => `${job.job_id}:${job.status}`).join(',');

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  const loadDocuments = useCallback(async signal => {
    setLoadingDocuments(true);
    try {
      const list = await request('/api/admin/documents', { signal });
      if (mounted.current) setDocuments(list);
    } catch (failure) {
      if (mounted.current && failure.name !== 'AbortError') setError(failure.message);
    } finally { if (mounted.current && !signal?.aborted) setLoadingDocuments(false); }
  }, [request]);

  useEffect(() => {
    const controller = new AbortController();
    loadDocuments(controller.signal);
    return () => controller.abort();
  }, [loadDocuments, completedKey]);

  useEffect(() => {
    if (tab !== 'users') return;
    const controller = new AbortController();
    request('/api/admin/users', { signal: controller.signal })
      .then(list => { if (!controller.signal.aborted) setUsers(list); })
      .catch(failure => { if (failure.name !== 'AbortError') setError(failure.message); });
    return () => controller.abort();
  }, [tab, request]);

  async function upload(event) {
    event.preventDefault();
    if (!file || uploading) return;
    setError('');
    setNotice('');
    if (!/\.(pdf|docx|doc)$/i.test(file.name)) {
      setError('אפשר להעלות קובצי PDF, DOCX או DOC בלבד.');
      return;
    }
    if (!file.size) { setError('הקובץ ריק. יש לבחור מסמך עם תוכן.'); return; }
    setUploading(true);
    try {
      const form = new FormData();
      form.append('file', file);
      const accepted = await request('/api/admin/documents', { method: 'POST', body: form });
      addJob(accepted, 'document', accepted.filename || file.name);
      if (!mounted.current) return;
      setNotice('המסמך התקבל לעיבוד. מצב העיבוד מופיע ברשימת הבקשות.');
      setFile(null);
      if (inputFile.current) inputFile.current.value = '';
    } catch (failure) { if (mounted.current) setError(failure.message); }
    finally { if (mounted.current) setUploading(false); }
  }

  async function remove(document) {
    if (deleting !== null) return;
    if (!window.confirm(`להסיר את המסמך "${document.source_name || document.topic_key}" ממאגר הידע? לאחר ההסרה התשובות לא ישתמשו בו.`)) return;
    setDeleting(document.id);
    setError('');
    setNotice('');
    try {
      await request(`/api/admin/documents/${document.id}`, { method: 'DELETE' });
      if (!mounted.current) return;
      setDocuments(current => current.filter(item => item.id !== document.id));
      setNotice('המסמך הוסר ממאגר הידע.');
    } catch (failure) { if (mounted.current) setError(failure.message); }
    finally { if (mounted.current) setDeleting(null); }
  }

  async function saveParameter(event) {
    event.preventDefault();
    if (savingParameter) return;
    setError('');
    setNotice('');
    setSavingParameter(true);
    try {
      const accepted = await request('/api/admin/parameters', {
        method: 'POST', body: JSON.stringify({ ...parameter, key: parameter.key.trim(), value: parameter.value.trim() }),
      });
      addJob(accepted, 'parameter', parameter.key.trim());
      if (mounted.current) setNotice('הערך נשמר ונשלח לעדכון במאגר הידע. אפשר לעקוב אחר העיבוד ברשימת הבקשות.');
    } catch (failure) { if (mounted.current) setError(failure.message); }
    finally { if (mounted.current) setSavingParameter(false); }
  }

  async function createUser(event) {
    event.preventDefault();
    if (savingUser) return;
    setError('');
    setNotice('');
    setSavingUser(true);
    try {
      const created = await request('/api/admin/users', {
        method: 'POST', body: JSON.stringify({ ...newUser, username: newUser.username.trim() }),
      });
      if (!mounted.current) return;
      setUsers(current => [...current, created].sort((a, b) => a.username.localeCompare(b.username)));
      setNewUser({ username: '', password: '', role: 'technician' });
      setNotice('המשתמש נוצר.');
    } catch (failure) { if (mounted.current) setError(failure.message); }
    finally { if (mounted.current) setSavingUser(false); }
  }

  function changeTab(next) { setError(''); setNotice(''); setTab(next); }

  return <section className="card admin-panel" aria-label="ניהול המערכת">
    <h2>ניהול מאגר הידע</h2>
    <div className="view-tabs admin-tabs" role="tablist" aria-label="ניהול">
      {[['documents', 'מסמכים'], ['parameters', 'ערכים מקצועיים'], ['users', 'משתמשים']].map(([key, title]) =>
        <button type="button" key={key} id={`tab-${key}`} role="tab" aria-selected={tab === key}
          aria-controls={`panel-${key}`} className={tab === key ? 'selected' : 'secondary'} onClick={() => changeTab(key)}>{title}</button>)}
    </div>
    {error && <p className="error-banner" role="alert">{error}</p>}
    {notice && <p className="success" role="status">{notice}</p>}
    {tab === 'documents' && <div role="tabpanel" id="panel-documents" aria-labelledby="tab-documents">
      <form className="upload-form" onSubmit={upload}>
        <label htmlFor="document-file">העלאת מסמך</label>
        <p className="muted">PDF או Word ‏(DOCX, DOC). המסמך יהיה זמין לשאלות לאחר שהעיבוד יושלם.</p>
        <input ref={inputFile} id="document-file" type="file" accept=".pdf,.docx,.doc,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/msword"
          disabled={uploading} onChange={event => setFile(event.target.files?.[0] || null)} />
        {file && <p className="file-summary"><bdi>{file.name}</bdi> · {(file.size / 1024 / 1024).toFixed(2)} MB</p>}
        <button disabled={!file || uploading}>{uploading ? 'מעלה לשרת…' : 'העלאה ועיבוד'}</button>
      </form>
      <div className="section-heading"><h3>מסמכים במאגר</h3>
        <button type="button" className="secondary" disabled={loadingDocuments} onClick={() => { setError(''); loadDocuments(); }}>
          {loadingDocuments ? 'טוען…' : 'רענון'}
        </button>
      </div>
      <div className="table-wrap"><table>
        <thead><tr><th>מסמך</th><th>גרסה</th><th>מצב</th><th>עודכן</th><th><span className="visually-hidden">פעולות</span></th></tr></thead>
        <tbody>{documents.map(document => <tr key={document.id}>
          <td><bdi>{document.source_name || document.topic_key}</bdi><small className="document-topic"><bdi>{document.topic_key}</bdi></small></td>
          <td><bdi>{document.version_label || '—'}</bdi></td>
          <td>{documentStatus[document.status] || document.status}</td>
          <td>{dateLabel(document.indexed_at)}</td>
          <td><button type="button" className="danger secondary" disabled={deleting !== null || document.status === 'deleting'} onClick={() => remove(document)}>
            {deleting === document.id ? 'מסיר…' : 'הסרה'}
          </button></td>
        </tr>)}</tbody>
      </table></div>
      {!documents.length && <p className="muted">{loadingDocuments ? 'טוען מסמכים…' : 'אין עדיין מסמכים במאגר.'}</p>}
    </div>}
    {tab === 'parameters' && <div role="tabpanel" id="panel-parameters" aria-labelledby="tab-parameters">
      <p className="muted">שמירת ערך מקצועי והסבר, כגון מספר VLAN או הגדרה ארגונית, כחלק ממאגר הידע.</p>
      <form className="admin-form" onSubmit={saveParameter}>
        <label htmlFor="parameter-key">שם הערך</label>
        <input id="parameter-key" required maxLength={120} value={parameter.key} disabled={savingParameter}
          onChange={event => setParameter(current => ({ ...current, key: event.target.value }))} />
        <label htmlFor="parameter-value">ערך</label>
        <textarea id="parameter-value" required maxLength={4000} rows={3} value={parameter.value} disabled={savingParameter}
          onChange={event => setParameter(current => ({ ...current, value: event.target.value }))} />
        <label htmlFor="parameter-description">הסבר</label>
        <textarea id="parameter-description" rows={3} maxLength={4000} value={parameter.description} disabled={savingParameter}
          onChange={event => setParameter(current => ({ ...current, description: event.target.value }))} />
        <button disabled={savingParameter || !parameter.key.trim() || !parameter.value.trim()}>{savingParameter ? 'שומר…' : 'שמירה ועדכון המאגר'}</button>
      </form>
    </div>}
    {tab === 'users' && <div role="tabpanel" id="panel-users" aria-labelledby="tab-users">
      <h3>יצירת משתמש</h3>
      <form className="admin-form" onSubmit={createUser}>
        <label htmlFor="new-username">שם משתמש</label>
        <input id="new-username" autoComplete="off" minLength={3} maxLength={80} required value={newUser.username} disabled={savingUser}
          onChange={event => setNewUser(current => ({ ...current, username: event.target.value }))} />
        <label htmlFor="new-password">סיסמה (12 תווים לפחות)</label>
        <input id="new-password" type="password" autoComplete="new-password" minLength={12} maxLength={256} required value={newUser.password} disabled={savingUser}
          onChange={event => setNewUser(current => ({ ...current, password: event.target.value }))} />
        <label htmlFor="new-role">תפקיד</label>
        <select id="new-role" value={newUser.role} disabled={savingUser}
          onChange={event => setNewUser(current => ({ ...current, role: event.target.value }))}>
          <option value="technician">טכנאי</option><option value="admin">מנהל</option>
        </select>
        <button disabled={savingUser || newUser.username.trim().length < 3 || newUser.password.length < 12}>{savingUser ? 'יוצר…' : 'יצירת משתמש'}</button>
      </form>
      <h3>משתמשי המערכת</h3>
      <div className="table-wrap"><table>
        <thead><tr><th>שם משתמש</th><th>תפקיד</th><th>מצב</th></tr></thead>
        <tbody>{users.map(user => <tr key={user.id}><td><bdi>{user.username}</bdi></td>
          <td>{user.role === 'admin' ? 'מנהל' : 'טכנאי'}</td><td>{user.active ? 'פעיל' : 'לא פעיל'}</td>
        </tr>)}</tbody>
      </table></div>
    </div>}
    {documentJobs.length > 0 && <div className="job-section" aria-live="polite">
      <h3>בקשות עיבוד אחרונות</h3>
      <ul className="job-list">{documentJobs.slice(-15).reverse().map(job => <li key={job.job_id}>
        <div><strong><bdi>{job.name || job.filename || 'עדכון מאגר'}</bdi></strong><small>{dateLabel(job.created_at)}</small></div>
        <span className={`status-badge ${job.status}`}>{jobLabel(job)}</span>
        {job.status === 'failed' && <p className="error" role="alert">{job.error || 'העיבוד נכשל. יש לבדוק את המסמך ולנסות שוב.'}</p>}
      </li>)}</ul>
    </div>}
  </section>;
}
