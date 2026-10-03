"""Compare a fresh coordinated recovery with its source, without exposing private rows.

Run snapshot before restoring, then verify in the recovery API container. This is
a deployment acceptance check; it does not modify the corpus or user accounts.
"""
import argparse
import hashlib
import json
import secrets
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, '/app')

import httpx
from sqlalchemy import delete, select

from app.config import get_settings
from app.db import (AuditEvent, AuthSession, ChatMessage, ChatSession, DocumentRegistry,
                    JobRecord, SystemParameter, User, db_session, now_utc)
from app.health import dependency_checks
from app.rag import RagService
from app.queueing import JobQueue
from app.security import hash_password


def digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str,
                     separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def snapshot():
    result = {'sql': {}}
    with db_session() as db:
        for model in (User, ChatSession, ChatMessage, DocumentRegistry,
                      SystemParameter, AuditEvent, JobRecord):
            columns = list(model.__table__.columns)
            rows = [dict(row._mapping) for row in db.execute(
                select(*columns).order_by(*model.__table__.primary_key.columns))]
            result['sql'][model.__tablename__] = {
                'rows': len(rows), 'sha256': digest(rows),
            }
    root = get_settings().data_path
    files = []
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if relative.parts[0] == 'ollama' or path.is_symlink() or not path.is_file():
            continue
        files.append((relative.as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()))
    result['documents'] = {'files': len(files), 'sha256': digest(files)}
    rag = RagService()
    vectors = rag.collection.get(include=['documents', 'metadatas'])
    rows = sorted(zip(vectors['ids'], vectors['documents'], vectors['metadatas']))
    result['vectors'] = {'chunks': len(rows), 'sha256': digest(rows)}
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['prepare', 'cleanup', 'snapshot', 'verify'])
    parser.add_argument('--expected', type=Path)
    parser.add_argument('--fixture', type=Path)
    parser.add_argument('--question')
    parser.add_argument('--contains')
    args = parser.parse_args()
    if args.mode == 'prepare':
        marker = uuid.uuid4().hex[:12]
        session_id, job_id = str(uuid.uuid4()), str(uuid.uuid4())
        question = 'Which VLAN, interface address and gateway does the management network use?'
        with db_session() as db:
            user = User(username='recovery-test-' + marker,
                        password_hash=hash_password(secrets.token_urlsafe(32)), role='technician')
            db.add(user); db.flush()
            db.add(ChatSession(id=session_id, user_id=user.id)); db.flush()
            db.add(ChatMessage(session_id=session_id, user_id=user.id, role='user', content='Which management VLAN?'))
            db.add(ChatMessage(session_id=session_id, user_id=user.id, role='assistant',
                               content='Management VLAN 120 [S1]',
                               sources=[{'id': 'S1', 'source': 'Network_Test_v1.docx'}]))
            db.flush()
            # The fixture is deliberately not runnable until recovery verification.
            JobQueue().enqueue('high', 'chat', {'session_id': session_id, 'user_id': user.id,
                               'message': question}, user_id=user.id, job_id=job_id, db=db)
            db.get(JobRecord, job_id).available_at = now_utc() + timedelta(days=1)
            metadata = {'user_id': user.id, 'username': user.username,
                        'session_id': session_id, 'job_id': job_id}
        print(json.dumps(metadata))
        return
    if args.mode == 'cleanup':
        if not args.fixture:
            parser.error('--fixture is required for cleanup')
        fixture = json.loads(args.fixture.read_text(encoding='utf-8-sig'))
        with db_session() as db:
            user = db.get(User, fixture['user_id'])
            if user and user.username == fixture['username'] and user.username.startswith('recovery-test-'):
                for model in (AuthSession, ChatMessage, ChatSession, AuditEvent, JobRecord):
                    db.execute(delete(model).where(model.user_id == user.id))
                db.delete(user)
        print('PASS removed only the private recovery fixture')
        return
    actual = snapshot()
    if args.mode == 'snapshot':
        print(json.dumps(actual, sort_keys=True))
        return
    if not args.expected:
        parser.error('--expected is required for verification')
    expected = json.loads(args.expected.read_text(encoding='utf-8-sig'))
    for section in expected:
        if expected[section] != actual.get(section):
            raise AssertionError(f'Recovery differs in {section}: expected {expected[section]}, got {actual.get(section)}')
        print('PASS recovered ' + section, flush=True)
    checks = dependency_checks(include_workers=True)
    if any(value != 'ok' for value in checks.values()):
        raise AssertionError('Recovery dependencies: ' + str(checks))
    print('PASS recovered services and configured local models', flush=True)
    if args.fixture:
        fixture = json.loads(args.fixture.read_text(encoding='utf-8-sig'))
        token = secrets.token_urlsafe(32)
        with db_session() as db:
            user = db.get(User, fixture['user_id'])
            if not user or user.username != fixture['username']:
                raise AssertionError('Recovery fixture ownership differs')
            db.add(AuthSession(token_hash=hashlib.sha256(token.encode()).hexdigest(),
                               user_id=user.id, expires_at=now_utc() + timedelta(minutes=10)))
        with httpx.Client(base_url='https://frontend', verify=False, trust_env=False,
                          timeout=15, cookies={'rag_auth': token}) as client:
            sid, jid = fixture['session_id'], fixture['job_id']
            history = client.get(f'/api/sessions/{sid}/history')
            history.raise_for_status()
            if len(history.json()) != 2 or not history.json()[-1]['sources']:
                raise AssertionError('Recovered history or its citations are missing')
            print('PASS recovered conversation history and citations over HTTPS', flush=True)
            with db_session() as db:
                db.get(JobRecord, jid).available_at = now_utc()
            JobQueue().notify('high', jid)
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                response = client.get('/api/jobs/' + jid)
                response.raise_for_status()
                job = response.json()
                if job['status'] in {'succeeded', 'failed'}:
                    break
                time.sleep(.5)
            else:
                raise AssertionError('Recovered pending job did not complete')
            result = job.get('result') or {}
            if job['status'] != 'succeeded' or not result.get('sources') or '120' not in result.get('answer', ''):
                raise AssertionError('Recovered pending job failed or lost evidence')
            if len(client.get(f'/api/sessions/{sid}/history').json()) != 3:
                raise AssertionError('Recovered job answer was not persisted exactly once')
            print('PASS recovered queued job, local Ollama answer and durable citations', flush=True)
    if args.question:
        result = RagService().answer(args.question, [])
        if not result['sources'] or (args.contains and args.contains not in result['answer']):
            raise AssertionError('Recovered answer did not match local evidence')
        print('PASS recovered local Ollama answer and citations', flush=True)


if __name__ == '__main__':
    main()
