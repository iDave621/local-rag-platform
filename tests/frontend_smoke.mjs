/**
 * Browser acceptance of the production UI against the documented API contract.
 * Run: npm --prefix frontend run build && node tests/frontend_smoke.mjs
 * Requires Node 22 and Chrome/Edge (set CHROME_PATH on other installations).
 * Only a temporary browser profile and in-memory fixture data are used.
 */
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { access, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { basename, dirname, extname, join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const project = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const dist = join(project, 'frontend', 'dist');
const pause = ms => new Promise(done => setTimeout(done, ms));
const temporary = await mkdtemp(join(tmpdir(), 'local-rag-ui-'));
const browserErrors = [];
const requests = [];
const account = { id: 7, username: 'test-admin', role: 'admin', active: true };
const state = {
  authorized: true, hold: false, expireOnce: false, pollUnavailable: false,
  failNextUpload: false, sequence: 0, resumeCount: 0, jobs: [], documents: [], users: [account],
  sessions: [{ session_id: 'history', title: 'שיחה קודמת', active: true,
    created_at: new Date().toISOString(), last_activity: new Date().toISOString() }],
  histories: new Map([['history', [
    { id: 1, role: 'user', content: 'שאלה קודמת', sources: [] },
    { id: 2, role: 'assistant', content: 'תשובה קודמת [S1]',
      sources: [{ id: 'S1', source: '/data/guide.pdf', page: 3, version: 'v2' }] },
  ]]]),
};

function complete(job) {
  if (!['queued', 'processing'].includes(job.status)) return;
  job.status = job.fail ? 'failed' : 'succeeded';
  job.error = job.fail ? 'Unreadable PDF fixture' : null;
  if (job.kind === 'chat' && !job.fail) {
    job.result = { answer: `תשובה לשאלה: ${job.question} [S1]`,
      sources: [{ id: 'S1', source: '/data/guide.pdf', page: 3, version: 'v2' }] };
    state.histories.get(job.session_id).push({ id: ++state.sequence, role: 'assistant',
      content: job.result.answer, sources: job.result.sources, job_id: job.job_id });
  } else if (job.kind === 'ingest' && !job.fail) {
    job.result = { indexed: 4 };
    state.documents.push({ id: ++state.sequence, source_name: job.filename,
      topic_key: `admin/${job.filename}`, version_label: 'v1', status: 'active', indexed_at: new Date().toISOString() });
  }
}

function enqueue(kind, values = {}) {
  const job = { job_id: `job-${++state.sequence}`, kind, status: 'queued',
    result: null, error: null, created_at: new Date().toISOString(), ...values };
  state.jobs.push(job);
  setTimeout(() => { if (job.status === 'queued') job.status = 'processing'; }, 100);
  setTimeout(() => { if (!state.hold) complete(job); }, 500);
  return job;
}

function publicJob(job) {
  const { question, request_id, fail, ...visible } = job;
  return visible;
}

const server = createServer(async (request, response) => {
  const url = new URL(request.url, 'http://127.0.0.1');
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  const raw = Buffer.concat(chunks);
  const body = request.headers['content-type']?.includes('application/json')
    ? JSON.parse(raw.toString() || '{}') : {};
  requests.push({ path: url.pathname, method: request.method, body });
  const send = (status, data, headers = {}) => {
    response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', ...headers });
    response.end(data == null ? '' : JSON.stringify(data));
  };
  try {
    if (url.pathname === '/api/auth/login') {
      state.authorized = true;
      return send(200, account, { 'Set-Cookie': 'fixture_auth=valid; HttpOnly; SameSite=Strict; Path=/' });
    }
    if (url.pathname.startsWith('/api/')) {
      if (!state.authorized || !request.headers.cookie?.includes('fixture_auth=valid')) return send(401, { detail: 'Authentication required' });
      if (url.pathname === '/api/auth/me') return send(200, account);
      if (url.pathname === '/api/auth/logout') return send(204, null, { 'Set-Cookie': 'fixture_auth=; Max-Age=0; Path=/' });
      if (url.pathname === '/api/sessions') {
        if (request.method === 'GET') return send(200, [...state.sessions].reverse());
        const session = { session_id: `conversation-${++state.sequence}`, title: 'New conversation', active: true,
          created_at: new Date().toISOString(), last_activity: new Date().toISOString() };
        state.sessions.push(session);
        state.histories.set(session.session_id, []);
        return send(201, session);
      }
      const sessionPath = url.pathname.match(/^\/api\/sessions\/([^/]+)\/(resume|history)$/);
      if (sessionPath) {
        if (sessionPath[2] === 'resume') {
          state.resumeCount++;
          return send(200, { session_id: sessionPath[1], active: true });
        }
        return send(200, state.histories.get(sessionPath[1]));
      }
      if (url.pathname === '/api/chat') {
        if (state.expireOnce) { state.expireOnce = false; return send(440, { detail: 'Session expired' }); }
        const existing = state.jobs.find(job => job.request_id === body.request_id);
        if (existing) return send(202, { job_id: existing.job_id, session_id: body.session_id });
        const pending = state.jobs.find(job => job.session_id === body.session_id && ['queued', 'processing'].includes(job.status));
        if (pending) return send(409, { detail: { message: 'Pending answer', job_id: pending.job_id } });
        const job = enqueue('chat', { question: body.message, request_id: body.request_id, session_id: body.session_id });
        state.histories.get(body.session_id).push({ id: ++state.sequence, role: 'user', content: body.message, sources: [], job_id: job.job_id });
        state.sessions.find(session => session.session_id === body.session_id).title = body.message;
        return send(202, { job_id: job.job_id, session_id: body.session_id });
      }
      if (url.pathname === '/api/jobs') {
        const sid = url.searchParams.get('session_id');
        return send(200, state.jobs.filter(job => !sid || job.session_id === sid).map(publicJob));
      }
      const jobPath = url.pathname.match(/^\/api\/jobs\/([^/]+)$/);
      if (jobPath) {
        if (state.pollUnavailable) return send(503, { detail: 'Temporary outage' });
        const job = state.jobs.find(item => item.job_id === jobPath[1]);
        return send(job ? 200 : 404, job ? publicJob(job) : { detail: 'Job not found' });
      }
      if (url.pathname === '/api/admin/documents') {
        if (request.method === 'GET') return send(200, state.documents);
        const filename = raw.toString().match(/filename="([^"]+)"/)?.[1] || 'unknown.pdf';
        const job = enqueue('ingest', { filename, origin: 'admin', fail: state.failNextUpload });
        state.failNextUpload = false;
        return send(202, { job_id: job.job_id, filename });
      }
      const documentPath = url.pathname.match(/^\/api\/admin\/documents\/(\d+)$/);
      if (documentPath && request.method === 'DELETE') {
        state.documents = state.documents.filter(item => item.id !== Number(documentPath[1]));
        return send(204, null);
      }
      if (url.pathname === '/api/admin/parameters') {
        const job = enqueue('ingest', { filename: body.key, origin: 'admin_parameter' });
        return send(202, { job_id: job.job_id });
      }
      if (url.pathname === '/api/admin/users') {
        if (request.method === 'GET') return send(200, state.users);
        const user = { id: ++state.sequence, username: body.username, role: body.role, active: true };
        state.users.push(user);
        return send(201, user);
      }
      return send(404, { detail: 'Missing fixture route' });
    }
    const requested = resolve(dist, `.${decodeURIComponent(url.pathname)}`);
    assert(requested.startsWith(resolve(dist) + sep) || requested === dist);
    let file;
    try { file = await readFile(requested); }
    catch { file = await readFile(join(dist, 'index.html')); }
    const mime = { '.js': 'application/javascript', '.css': 'text/css', '.html': 'text/html', '.svg': 'image/svg+xml' };
    response.writeHead(200, { 'Content-Type': `${mime[extname(requested)] || 'text/html'}; charset=utf-8` });
    response.end(file);
  } catch (failure) { send(500, { detail: failure.message }); }
});

await new Promise(done => server.listen(0, '127.0.0.1', done));
const origin = `http://127.0.0.1:${server.address().port}`;
let browser;
let socket;

try {
  await access(join(dist, 'index.html'));
  const candidates = [process.env.CHROME_PATH,
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    '/usr/bin/chromium', '/usr/bin/google-chrome'].filter(Boolean);
  let executable;
  for (const candidate of candidates) { try { await access(candidate); executable = candidate; break; } catch {} }
  assert(executable, 'Chrome/Edge is required. Set CHROME_PATH.');
  const profile = join(temporary, 'profile');
  browser = spawn(executable, ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-background-networking', '--disable-component-update', '--disable-sync', '--no-proxy-server',
    '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost',
    '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'], { windowsHide: true, stdio: 'ignore' });
  browser.on('error', error => browserErrors.push(error.message));
  let port;
  for (let attempt = 0; attempt < 100; attempt++) {
    try { port = (await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]; break; }
    catch { await pause(100); }
  }
  assert(port, `Browser failed to start: ${browserErrors.join('; ')}`);
  const target = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: 'PUT' })).json();
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((done, reject) => { socket.onopen = done; socket.onerror = reject; });
  let sequence = 0;
  const commands = new Map();
  function command(method, params = {}) {
    const id = ++sequence;
    return new Promise((done, reject) => {
      commands.set(id, { done, reject });
      socket.send(JSON.stringify({ id, method, params }));
    });
  }
  socket.onmessage = event => {
    const message = JSON.parse(event.data);
    if (message.id) {
      const pending = commands.get(message.id);
      commands.delete(message.id);
      if (message.error) pending?.reject(new Error(message.error.message));
      else pending?.done(message.result);
    } else if (message.method === 'Runtime.exceptionThrown') {
      browserErrors.push(message.params.exceptionDetails.exception?.description || message.params.exceptionDetails.text);
    } else if (message.method === 'Page.javascriptDialogOpening') {
      command('Page.handleJavaScriptDialog', { accept: true }).catch(() => {});
    }
  };
  await command('Page.enable');
  await command('Runtime.enable');
  async function evaluate(expression) {
    const result = await command('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
    return result.result.value;
  }
  async function until(expression, label) {
    const deadline = Date.now() + 20000;
    while (Date.now() < deadline) { if (await evaluate(expression)) return; await pause(100); }
    throw new Error(`Timed out: ${label}\n${await evaluate('document.body.innerText')}`);
  }
  async function fill(selector, value) {
    await evaluate(`(() => {
      const element = document.querySelector(${JSON.stringify(selector)});
      if (!element) throw Error('Missing input');
      const prototype = element.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : element.tagName === 'SELECT' ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(prototype, 'value').set.call(element, ${JSON.stringify(value)});
      element.dispatchEvent(new Event('input', { bubbles: true }));
      element.dispatchEvent(new Event('change', { bubbles: true }));
    })()`);
  }
  const click = selector => evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);
  const button = title => evaluate(`(() => {
    const element = [...document.querySelectorAll('button')].find(button => button.textContent.trim() === ${JSON.stringify(title)});
    if (!element) throw Error('Missing button'); element.click();
  })()`);
  const bodyIncludes = text => `document.body.innerText.includes(${JSON.stringify(text)})`;
  async function login() {
    await until('Boolean(document.querySelector("#username"))', 'login form');
    await fill('#username', 'test-admin');
    await fill('#password', 'fixture-password-long');
    await click('.login-card button');
    await until('Boolean(document.querySelector("#question:not(:disabled)"))', 'conversation ready');
  }
  async function submit(question) {
    await fill('#question', question);
    await until('!document.querySelector(".composer button").disabled', 'send enabled');
    await click('.composer button');
  }
  async function uploadFile(name) {
    const path = join(temporary, name);
    await writeFile(path, '%PDF-1.4\nfixture');
    const root = await command('DOM.getDocument');
    const node = await command('DOM.querySelector', { nodeId: root.root.nodeId, selector: '#document-file' });
    await command('DOM.setFileInputFiles', { nodeId: node.nodeId, files: [path] });
    await until('!document.querySelector(".upload-form button").disabled', 'upload enabled');
    await click('.upload-form button');
  }

  await command('Page.navigate', { url: origin });
  await login();
  await until(bodyIncludes('תשובה קודמת'), 'restored persisted history');
  await click('.sources summary');
  assert(await evaluate(bodyIncludes('guide.pdf')));
  assert(await evaluate(bodyIncludes('עמוד 3')));
  assert(await evaluate(bodyIncludes('[S1]')));
  console.log('PASS: login, history and evidence filenames/pages/citation IDs');

  await button('שיחה חדשה');
  await until('document.querySelector("#question") && !document.querySelector("#question").disabled && !document.querySelector(".message")', 'new empty conversation');
  state.hold = true;
  await submit('בדיקת רענון');
  await until(bodyIncludes('מכין תשובה'), 'pending response');
  const current = state.sessions.at(-1).session_id;
  const count = state.sessions.length;
  await command('Page.reload');
  await until(bodyIncludes('מכין תשובה'), 'pending response restored on reload');
  assert.equal(state.sessions.length, count, 'Reload must not create a conversation');
  await evaluate('localStorage.clear()');
  await command('Page.reload');
  await until(bodyIncludes('מכין תשובה'), 'server jobs restore without browser storage');
  state.hold = false;
  state.jobs.filter(job => job.session_id === current).forEach(complete);
  await until(bodyIncludes('תשובה לשאלה: בדיקת רענון'), 'durable response completed');
  assert.equal(await evaluate('document.querySelectorAll(".message.user").length'), 1);
  assert.equal(await evaluate('document.querySelectorAll(".message.assistant").length'), 1);
  assert.equal(state.histories.get(current).length, 2);
  console.log('PASS: queued chat survives reload and storage loss without duplicate history');

  const resumed = state.resumeCount;
  state.expireOnce = true;
  await submit('בדיקת חידוש');
  await until(bodyIncludes('תשובה לשאלה: בדיקת חידוש'), 'expired session resumed');
  assert.equal(state.resumeCount, resumed + 1);
  assert(requests.filter(request => request.path === '/api/chat').every(request => request.body.request_id), 'Each send needs an idempotency UUID');
  console.log('PASS: timed-out conversation resumes and sends an idempotency UUID');

  state.hold = true;
  const external = enqueue('chat', { session_id: current, question: 'בקשה מחלון אחר' });
  state.histories.get(current).push({ id: ++state.sequence, role: 'user', content: external.question, sources: [], job_id: external.job_id });
  await submit('טיוטה נוספת');
  await until(bodyIncludes('כבר נשלחה בקשה'), 'concurrent browser pending job recovered');
  complete(external);
  state.hold = false;
  await until(bodyIncludes('תשובה לשאלה: בקשה מחלון אחר'), 'concurrent pending answer retrieved');
  assert.equal(state.histories.get(current).filter(message => message.content === 'טיוטה נוספת').length, 0);
  console.log('PASS: concurrent conversation 409 recovers the existing job');

  state.hold = true;
  await submit('בדיקת חיבור');
  await until(bodyIncludes('מכין תשובה'), 'pending request before network interruption');
  state.pollUnavailable = true;
  await until(bodyIncludes('עדכון מצב הבקשות נעצר'), 'poll outage shown');
  state.pollUnavailable = false;
  state.hold = false;
  state.jobs.filter(job => job.session_id === current).forEach(complete);
  await until(bodyIncludes('תשובה לשאלה: בדיקת חיבור'), 'polling recovers');
  console.log('PASS: temporary status outage recovers without discarding work');

  state.hold = true;
  await submit('בדיקת כניסה');
  await until(bodyIncludes('מכין תשובה'), 'pending request before auth expiry');
  state.authorized = false;
  await until('Boolean(document.querySelector("#username"))', 'auth expiry returns to login');
  state.hold = false;
  state.jobs.filter(job => job.session_id === current).forEach(complete);
  await login();
  await until(bodyIncludes('תשובה לשאלה: בדיקת כניסה'), 'answer survives auth renewal');
  assert.equal(state.sessions.length, count);
  console.log('PASS: expired authentication preserves server history and pending work');

  await button('ניהול');
  await until('Boolean(document.querySelector("#document-file"))', 'admin document panel');
  const accept = await evaluate('document.querySelector("#document-file").accept');
  assert(accept.includes('.pdf') && accept.includes('.docx') && accept.includes('.doc'));
  state.hold = true;
  await uploadFile('manual.pdf');
  await until(bodyIncludes('manual.pdf'), 'upload tracked');
  await command('Page.reload');
  await until('Boolean(document.querySelector("#question:not(:disabled)"))', 'reload authenticated');
  await button('ניהול');
  await until(bodyIncludes('manual.pdf'), 'upload status restored');
  state.hold = false;
  state.jobs.filter(job => job.kind === 'ingest').forEach(complete);
  await until('document.querySelector("#panel-documents tbody")?.innerText.includes("manual.pdf")', 'indexed document listed');
  console.log('PASS: PDF/DOCX/DOC picker, upload status survives reload, document list refreshes');

  state.failNextUpload = true;
  await uploadFile('broken.pdf');
  await until(bodyIncludes('Unreadable PDF fixture'), 'indexing failure shown');
  await click('#panel-documents tbody button');
  await until('!document.querySelector("#panel-documents tbody")?.innerText.includes("manual.pdf")', 'document removed after confirmation');
  assert.equal(state.documents.length, 0);
  console.log('PASS: failed ingestion is visible and document removal refreshes the list');

  await click('#tab-parameters');
  await fill('#parameter-key', 'VLAN');
  await fill('#parameter-value', '120');
  await fill('#parameter-description', 'רשת ארגונית');
  await click('#panel-parameters button');
  await until(bodyIncludes('הערך נשמר'), 'parameter update submitted');
  assert.equal(requests.findLast(request => request.path === '/api/admin/parameters').body.value, '120');
  await click('#tab-users');
  await fill('#new-username', 'new-tech');
  await fill('#new-password', 'new-user-password-long');
  await click('#panel-users button');
  await until('document.querySelector("#panel-users tbody")?.innerText.includes("new-tech")', 'new user displayed');
  assert.equal(await evaluate('document.querySelector("#new-password").value'), '');
  console.log('PASS: knowledge parameters and account creation report their results');

  await command('Emulation.setDeviceMetricsOverride', { width: 375, height: 812, deviceScaleFactor: 1, mobile: true });
  await pause(200);
  assert(await evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Mobile UI should not overflow horizontally');
  const browserStorage = await evaluate('JSON.stringify(Object.fromEntries(Object.keys(localStorage).map(key => [key, localStorage.getItem(key)])))');
  assert(!browserStorage.includes('fixture-password') && !browserStorage.includes('תשובה לשאלה'), 'Stored job references must not contain passwords or conversation content');
  await button('יציאה');
  await until('Boolean(document.querySelector("#username"))', 'logout returns to login');
  assert.equal(browserErrors.length, 0, browserErrors.join('\n'));
  console.log('PASS: mobile layout, private storage and logout; no uncaught browser exceptions');
  console.log('Frontend browser acceptance passed.');
} finally {
  socket?.close();
  if (browser && browser.exitCode == null) {
    browser.kill();
    await Promise.race([new Promise(done => browser.once('exit', done)), pause(3000)]);
  }
  await new Promise(done => server.close(done));
  const target = resolve(temporary);
  assert(target.startsWith(resolve(tmpdir()) + sep) && basename(target).startsWith('local-rag-ui-'));
  await rm(target, { recursive: true, force: true, maxRetries: 6, retryDelay: 500 });
}
