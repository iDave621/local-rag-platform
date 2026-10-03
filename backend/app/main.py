import asyncio
import hashlib
import logging
import re
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import select, text

from .config import get_settings
from .db import (AuditEvent, ChatMessage, ChatSession, DocumentRegistry, JobRecord,
                 SystemParameter, User, as_utc, db_session, init_db, now_utc)
from .health import dependency_checks
from .ingestion import validate_document
from .queueing import JobQueue, RateLimitExceeded, consume_rate_limit
from .schemas import ChatRequest, CreateUserIn, LoginRequest, ParameterIn
from .security import authenticate, current_user, hash_password, logout, require_admin
from .versioning import identify_version

s = get_settings()
q = JobQueue()
log = logging.getLogger(__name__)
app = FastAPI(title='Local RAG Platform', version='2.2.0', docs_url='/api/docs')


@app.on_event('startup')
def startup():
    init_db()
    s.data_path.mkdir(parents=True, exist_ok=True)


@app.get('/api/health')
def health():
    checks = dependency_checks(include_workers=True)
    ready = all(value == 'ok' for value in checks.values())
    return JSONResponse({'api': 'ok' if ready else 'not_ready', 'mode': 'local-offline',
                         'checks': checks}, status_code=200 if ready else 503)


@app.get('/api/live')
def live():
    return {'api': 'ok'}


def limit(identity, action, maximum):
    try:
        consume_rate_limit(identity, action, maximum)
    except RateLimitExceeded:
        raise HTTPException(429, 'Rate limit exceeded; try again shortly', headers={'Retry-After': '60'})


@app.post('/api/auth/login')
def login(payload: LoginRequest, request: Request, response: Response):
    # nginx overwrites X-Real-IP; the API is not published outside the Docker network.
    address = request.headers.get('x-real-ip') or (request.client.host if request.client else 'unknown')
    limit(address, 'login-ip', s.login_rate_limit_per_minute)
    limit(payload.username.lower(), 'login-account', s.login_account_rate_limit_per_minute)
    user, token = authenticate(payload.username, payload.password)
    response.set_cookie('rag_auth', token, max_age=43200, httponly=True,
                        secure=s.cookie_secure, samesite='strict', path='/')
    return user


@app.post('/api/auth/logout', status_code=204)
def sign_out(request: Request, response: Response):
    logout(request.cookies.get('rag_auth'))
    response.delete_cookie('rag_auth', path='/', secure=s.cookie_secure, httponly=True, samesite='strict')


@app.get('/api/auth/me')
def me(request: Request):
    return current_user(request)


@app.get('/api/sessions')
def sessions(request: Request):
    user = current_user(request)
    with db_session() as db:
        rows = db.scalars(select(ChatSession).where(ChatSession.user_id == user['id'])
                          .order_by(ChatSession.last_activity.desc()).limit(100)).all()
        result = []
        for row in rows:
            first = db.scalar(select(ChatMessage).where(ChatMessage.session_id == row.id,
                              ChatMessage.role == 'user').order_by(ChatMessage.id).limit(1))
            pending = db.scalar(select(JobRecord.id).where(JobRecord.session_id == row.id,
                                JobRecord.status.in_(['queued', 'processing'])))
            result.append({'session_id': row.id, 'active': row.active,
                           'created_at': row.created_at.isoformat(),
                           'last_activity': row.last_activity.isoformat(),
                           'title': first.content[:70] if first else 'New conversation',
                           'pending_job_id': pending})
        return result


@app.post('/api/sessions', status_code=201)
def new_session(request: Request):
    user = current_user(request)
    sid = str(uuid.uuid4())
    with db_session() as db:
        db.add(ChatSession(id=sid, user_id=user['id'], active=True))
    return {'session_id': sid, 'active': True, 'inactivity_timeout_seconds': s.session_timeout_seconds}


@app.post('/api/sessions/{sid}/resume')
def resume(sid: str, request: Request):
    user = current_user(request)
    with db_session() as db:
        row = db.get(ChatSession, sid)
        if not row or row.user_id != user['id']:
            raise HTTPException(404, 'Session not found')
        row.active = True
        row.last_activity = now_utc()
    return {'session_id': sid, 'active': True, 'inactivity_timeout_seconds': s.session_timeout_seconds}


@app.get('/api/sessions/{sid}/history')
def history(sid: str, request: Request):
    user = current_user(request)
    with db_session() as db:
        row = db.get(ChatSession, sid)
        if not row or row.user_id != user['id']:
            raise HTTPException(404, 'Session not found')
        return [{'id': message.id, 'role': message.role, 'content': message.content,
                 'sources': message.sources or [], 'job_id': message.job_id,
                 'created_at': message.created_at.isoformat()}
                for message in db.scalars(select(ChatMessage).where(ChatMessage.session_id == sid)
                                          .order_by(ChatMessage.id)).all()]


@app.post('/api/chat', status_code=202)
def chat(payload: ChatRequest, request: Request):
    user = current_user(request)
    if user['role'] == 'technician' and len(payload.message) > s.technician_max_message_chars:
        raise HTTPException(413, 'Message too large')
    limit(user['id'], 'chat', s.admin_rate_limit_per_minute if user['role'] == 'admin'
          else s.technician_rate_limit_per_minute)
    jid = str(uuid.uuid4())
    key = f'chat:{user["id"]}:{payload.request_id}' if payload.request_id else None
    with db_session() as db:
        if key and db.bind.dialect.name == 'postgresql':
            lock_id = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], 'big', signed=True)
            db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': lock_id})
        session = db.scalar(select(ChatSession).where(ChatSession.id == payload.session_id).with_for_update())
        if not session or session.user_id != user['id']:
            raise HTTPException(404, 'Session not found')
        if key:
            previous = db.scalar(select(JobRecord).where(JobRecord.idempotency_key == key))
            if previous:
                if previous.session_id != session.id or previous.payload.get('message') != payload.message:
                    raise HTTPException(409, 'Request ID already used for a different message or conversation')
                return {'job_id': previous.id, 'session_id': session.id}
        pending = db.scalar(select(JobRecord).where(JobRecord.session_id == session.id,
                            JobRecord.status.in_(['queued', 'processing'])))
        if pending:
            raise HTTPException(409, {'message': 'This conversation already has a pending answer',
                                      'job_id': pending.id})
        if (now_utc() - as_utc(session.last_activity)).total_seconds() > s.session_timeout_seconds:
            raise HTTPException(440, 'Session expired; resume the conversation')
        session.last_activity = now_utc()
        session.active = True
        message = ChatMessage(session_id=session.id, user_id=user['id'],
                              role='user', content=payload.message, job_id=jid)
        db.add(message)
        db.flush()
        q.enqueue('high', 'chat', {'session_id': session.id, 'user_id': user['id'],
                  'message': payload.message, 'history_before_id': message.id},
                  user_id=user['id'], idempotency_key=key, db=db, job_id=jid)
    q.notify('high', jid)
    return {'job_id': jid, 'session_id': payload.session_id}


@app.get('/api/jobs/{jid}')
def job_status(jid: str, request: Request):
    user = current_user(request)
    row = q.status(jid)
    if not row or (row['user_id'] != user['id'] and user['role'] != 'admin'):
        raise HTTPException(404, 'Job not found')
    return row


@app.get('/api/jobs')
def jobs(request: Request, session_id: str | None = None):
    user = current_user(request)
    with db_session() as db:
        query = select(JobRecord).where(JobRecord.user_id == user['id'])
        if session_id:
            query = query.where(JobRecord.session_id == session_id)
        pending = db.scalars(query.where(JobRecord.status.in_(['queued', 'processing']))
                             .order_by(JobRecord.created_at.desc())).all()
        completed = db.scalars(query.where(JobRecord.status.in_(['succeeded', 'failed']))
                               .order_by(JobRecord.created_at.desc()).limit(100)).all()
        ids = pending + completed
    return [q.status(row.id) for row in ids]


def safe_filename(raw):
    name = (raw or '').replace('\\', '/').split('/')[-1]
    if not name or len(name) > 200 or any(ord(char) < 32 for char in name):
        raise HTTPException(400, 'Invalid filename')
    return name


@app.post('/api/admin/documents', status_code=202)
async def upload(request: Request, file: UploadFile = File(...)):
    user = require_admin(current_user(request))
    limit(user['id'], 'upload', s.admin_rate_limit_per_minute)
    name = safe_filename(file.filename)
    if Path(name).suffix.lower() not in {'.pdf', '.docx', '.doc'}:
        raise HTTPException(415, 'Supported documents: PDF, DOCX and DOC')
    data = await file.read(s.admin_upload_max_bytes + 1)
    if not data:
        raise HTTPException(400, 'Empty document')
    if len(data) > s.admin_upload_max_bytes:
        raise HTTPException(413, 'File too large')
    folder = s.data_path / 'staging' / 'admin' / uuid.uuid4().hex
    folder.mkdir(parents=True, exist_ok=False)
    path = folder / name
    path.write_bytes(data)
    try:
        await asyncio.to_thread(validate_document, path, s.admin_docx_max_uncompressed_bytes)
        identity = identify_version(name, 'admin')
        jid = q.enqueue('medium', 'ingest', {'path': str(path), 'origin': 'admin',
                        'source_name': name, 'topic_key': identity.topic_key,
                        'version_rank': identity.version_rank, 'version_label': identity.version_label},
                        user_id=user['id'])
    except (ValueError, OSError) as error:
        path.unlink(missing_ok=True)
        folder.rmdir()
        raise HTTPException(400, str(error))
    except Exception:
        path.unlink(missing_ok=True)
        folder.rmdir()
        raise
    with db_session() as db:
        db.add(AuditEvent(user_id=user['id'], action='document.upload', detail=name))
    return {'job_id': jid, 'filename': name}


@app.get('/api/admin/documents')
def documents(request: Request):
    require_admin(current_user(request))
    with db_session() as db:
        return [{'id': row.id, 'source_name': Path(row.source).name,
                 'topic_key': row.topic_key, 'version_label': row.version_label,
                 'status': row.status, 'indexed_at': row.indexed_at.isoformat(),
                 'generation_id': row.generation_id, 'chunks': row.chunk_count}
                for row in db.scalars(select(DocumentRegistry).where(
                    DocumentRegistry.status.in_(['active', 'staging']))
                    .order_by(DocumentRegistry.indexed_at.desc())).all()]


@app.delete('/api/admin/documents/{document_id}', status_code=204)
def remove_document(document_id: int, request: Request):
    user = require_admin(current_user(request))
    from .ingestion import topic_lock
    with db_session() as db:
        row = db.get(DocumentRegistry, document_id)
        if not row or row.status not in {'active', 'staging'}:
            raise HTTPException(404, 'Document not found')
        topic_lock(db, row.topic_key)
        rows = db.scalars(select(DocumentRegistry).where(DocumentRegistry.topic_key == row.topic_key,
                          DocumentRegistry.status.in_(['active', 'staging'])).with_for_update()).all()
        generations = [item.generation_id for item in rows]
        for item in rows:
            item.status = 'deleted'
        pending = db.scalars(select(JobRecord).where(
            JobRecord.kind == 'ingest', JobRecord.status.in_(['queued', 'processing']),
            JobRecord.payload['topic_key'].as_string() == row.topic_key
        ).with_for_update()).all()
        for job in pending:
            job.status = 'failed'
            job.error = 'Document removed from the knowledge base'
            job.claim_token = None
            job.lease_until = None
            job.updated_at = now_utc()
        db.add(AuditEvent(user_id=user['id'], action='document.remove', detail=row.topic_key))
    # SQL retirement hides the document even if Chroma is temporarily unavailable.
    try:
        from .rag import RagService
        rag = RagService()
        for generation in generations:
            rag.delete_generation(generation)
    except Exception:
        log.warning('Deferred vector cleanup for removed document %s', document_id)


@app.post('/api/admin/parameters', status_code=202)
def parameter(payload: ParameterIn, request: Request):
    user = require_admin(current_user(request))
    name = hashlib.sha256(payload.key.encode()).hexdigest()[:16] + '.txt'
    folder = s.data_path / 'staging' / 'parameters' / uuid.uuid4().hex
    folder.mkdir(parents=True)
    path = folder / name
    path.write_text(f'{payload.key} = {payload.value}\n{payload.description}', encoding='utf-8')
    with db_session() as db:
        row = db.get(SystemParameter, payload.key)
        if row:
            row.value = payload.value
            row.description = payload.description
            row.updated_by = user['id']
            row.updated_at = now_utc()
        else:
            db.add(SystemParameter(key=payload.key, value=payload.value,
                                   description=payload.description, updated_by=user['id']))
        jid = q.enqueue('medium', 'ingest', {'path': str(path), 'origin': 'admin_parameter',
                        'source_name': name, 'topic_key': f'parameters/{name}',
                        'version_rank': 0, 'version_label': 'current'}, user_id=user['id'], db=db)
    q.notify('medium', jid)
    return {'job_id': jid}


@app.get('/api/admin/users')
def users(request: Request):
    require_admin(current_user(request))
    with db_session() as db:
        return [{'id': row.id, 'username': row.username, 'role': row.role, 'active': row.active}
                for row in db.scalars(select(User).order_by(User.username)).all()]


@app.post('/api/admin/users', status_code=201)
def add_user(payload: CreateUserIn, request: Request):
    admin = require_admin(current_user(request))
    with db_session() as db:
        if db.scalar(select(User).where(User.username == payload.username)):
            raise HTTPException(409, 'User exists')
        row = User(username=payload.username, password_hash=hash_password(payload.password),
                   role=payload.role, active=True)
        db.add(row)
        db.flush()
        db.add(AuditEvent(user_id=admin['id'], action='user.create', detail=payload.username))
        return {'id': row.id, 'username': row.username, 'role': row.role, 'active': row.active}
