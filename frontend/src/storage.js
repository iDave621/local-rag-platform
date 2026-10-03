const key = (userId, name) => `local-rag:${userId}:${name}`;

export function readStored(userId, name, fallback) {
  try {
    const value = localStorage.getItem(key(userId, name));
    return value ? JSON.parse(value) : fallback;
  } catch { return fallback; }
}

export function writeStored(userId, name, value) {
  try { localStorage.setItem(key(userId, name), JSON.stringify(value)); }
  catch { /* Conversations and job results remain available on the server. */ }
}

export function rememberJobs(userId, jobs) {
  // Store only job references, never message bodies, answers or credentials.
  const active = jobs.filter(job => ['queued', 'processing'].includes(job.status));
  const finished = jobs.filter(job => !['queued', 'processing'].includes(job.status)).slice(-30);
  writeStored(userId, 'jobs', [...finished, ...active].map(job => ({
    job_id: job.job_id, session_id: job.session_id, kind: job.kind,
    name: job.name, status: job.status, created_at: job.created_at,
    error: job.error,
  })));
}
