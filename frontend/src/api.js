export class ApiError extends Error {
  constructor(message, status, detail) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

const statusMessages = {
  401: 'הכניסה למערכת פגה. יש להתחבר מחדש.',
  403: 'אין לך הרשאה לפעולה הזאת.',
  404: 'הפריט המבוקש אינו קיים.',
  413: 'התוכן גדול מדי. יש להקטין אותו ולנסות שוב.',
  415: 'סוג הקובץ אינו נתמך. אפשר להעלות PDF, DOCX או DOC.',
  429: 'בוצעו יותר מדי בקשות. יש להמתין מעט ולנסות שוב.',
  503: 'השרת אינו זמין כרגע. יש לנסות שוב בעוד מעט.',
};

export async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      credentials: 'include',
      ...options,
      headers: {
        ...(options.body && !(options.body instanceof FormData)
          ? { 'Content-Type': 'application/json' }
          : {}),
        ...options.headers,
      },
    });
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new ApiError('אין חיבור לשרת המקומי. יש לבדוק את החיבור לרשת ולנסות שוב.', 0);
  }
  if (!response.ok) {
    let detail;
    try { detail = (await response.json()).detail; }
    catch { /* The proxy can return a non-JSON error response. */ }
    const message = statusMessages[response.status]
      || (typeof detail === 'string' ? detail : `הבקשה נכשלה (${response.status}).`);
    throw new ApiError(message, response.status, detail);
  }
  return response.status === 204 ? null : response.json();
}

export const isPending = job => job.status === 'queued' || job.status === 'processing';
export const jobLabel = job => ({
  queued: 'ממתין בתור', processing: 'בעיבוד', succeeded: 'הושלם', failed: 'נכשל',
}[job.status] || job.status);

export function mergeJobs(current, serverJobs) {
  const byId = new Map(current.map(job => [job.job_id, job]));
  for (const job of serverJobs) {
    const previous = byId.get(job.job_id);
    const kind = job.kind === 'ingest'
      ? (job.origin === 'admin_parameter' ? 'parameter' : 'document')
      : job.kind;
    byId.set(job.job_id, { ...previous, ...job, kind, name: previous?.name || job.filename });
  }
  return [...byId.values()].sort((a, b) => (a.created_at || '').localeCompare(b.created_at || ''));
}

export function dateLabel(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? '' : date.toLocaleString('he-IL');
}

export function sourceLabel(source) {
  const name = source.source_name || source.source || 'מסמך';
  return name.split(/[\\/]/).pop();
}
