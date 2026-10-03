"""Regression tests use isolated SQLite and fake unavailable Redis, never project data."""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

os.environ['DATABASE_URL'] = 'sqlite://'
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

import redis
from docx import Document
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import db as database, main, queueing
from app.cleanup_worker import cleanup_once
from app.db import (AuthSession, Base, ChatMessage, ChatSession, DocumentRegistry,
                    JobRecord, User, db_session, now_utc)
from app.queueing import JobQueue, LeaseLostError, RateLimitExceeded
from app.security import hash_password
from app.worker import dispatch
from app.worker import touch_worker, retire_worker
from app.health import local_worker_ready, worker_ready


class BackendRegressionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.patches = [patch.object(database, 'engine', self.engine),
                        patch.object(database, 'SessionLocal', self.factory)]
        self.redis = Mock()
        self.redis.lpush.side_effect = redis.ConnectionError('offline')
        self.redis.eval.side_effect = redis.ConnectionError('offline')
        self.patches.append(patch.object(queueing, 'redis_client', return_value=self.redis))
        for item in self.patches:
            item.start()
        Base.metadata.create_all(self.engine)
        self.queue = JobQueue()
        self.old_queue = main.q
        main.q = self.queue
        self.temp = tempfile.TemporaryDirectory()
        self.old_root = main.s.data_root
        main.s.data_root = self.temp.name
        self.client = TestClient(main.app, base_url='https://testserver')
        with db_session() as db:
            admin = User(username='test-admin', password_hash=hash_password('test-password-123'), role='admin')
            other = User(username='other-user', password_hash=hash_password('test-password-456'), role='technician')
            db.add_all([admin, other]); db.flush()
            self.user_id, self.other_id = admin.id, other.id
            for token, user_id in [('admin-token', admin.id), ('other-token', other.id)]:
                db.add(AuthSession(token_hash=hashlib.sha256(token.encode()).hexdigest(), user_id=user_id,
                                   expires_at=now_utc() + timedelta(hours=1)))
        self.client.cookies.set('rag_auth', 'admin-token')

    def tearDown(self):
        self.client.close()
        main.q = self.old_queue
        main.s.data_root = self.old_root
        self.temp.cleanup()
        for item in reversed(self.patches):
            item.stop()
        self.engine.dispose()

    def session(self):
        response = self.client.post('/api/sessions')
        self.assertEqual(response.status_code, 201)
        return response.json()['session_id']

    def test_job_survives_redis_failure_and_reclaims_crashed_worker(self):
        jid = self.queue.enqueue('medium', 'ingest', {'path': '/tmp/test.pdf'}, user_id=self.user_id)
        first = self.queue.claim(['medium'])
        self.assertEqual(first['id'], jid)
        with db_session() as db:
            db.get(JobRecord, jid).lease_until = now_utc() - timedelta(seconds=1)
        second = self.queue.claim(['medium'])
        self.assertEqual(second['attempts'], 2)
        with self.assertRaises(LeaseLostError):
            self.queue.complete(first, {'status': 'indexed'})
        self.queue.complete(second, {'status': 'indexed'})
        self.assertEqual(self.queue.status(jid)['result']['status'], 'indexed')
        self.assertIsNone(self.queue.claim(['medium']))

    def test_failed_jobs_are_visible_and_retries_are_bounded(self):
        jid = self.queue.enqueue('medium', 'ingest', {'path': 'missing.pdf'})
        with db_session() as db:
            db.get(JobRecord, jid).max_attempts = 2
        first = self.queue.claim(['medium'])
        self.queue.fail(first, 'dependency unavailable')
        with db_session() as db:
            db.get(JobRecord, jid).available_at = now_utc() - timedelta(seconds=1)
        second = self.queue.claim(['medium'])
        self.queue.fail(second, 'still unavailable')
        status = self.queue.status(jid)
        self.assertEqual(status['status'], 'failed')
        self.assertEqual(status['error'], 'still unavailable')

    def test_final_attempt_recovers_an_answer_saved_before_acknowledgement(self):
        sid = self.session()
        response = self.client.post('/api/chat', json={'session_id': sid, 'message': 'Which VLAN?'})
        jid = response.json()['job_id']
        with db_session() as db:
            db.get(JobRecord, jid).max_attempts = 1
        job = self.queue.claim(['high'])
        rag = Mock()
        rag.answer.return_value = {'answer': 'VLAN 120 [S1]', 'sources': [{'id': 'S1', 'source': 'guide.pdf'}]}
        dispatch(job, self.queue, lambda: rag)
        # The process dies after its answer transaction commits, before complete().
        with db_session() as db:
            db.get(JobRecord, jid).lease_until = now_utc() - timedelta(seconds=1)
        self.assertIsNone(self.queue.claim(['high']))
        status = self.queue.status(jid)
        self.assertEqual(status['status'], 'succeeded')
        self.assertEqual(status['result'], rag.answer.return_value)
        self.assertIsNone(status['error'])
        self.assertEqual(len(self.client.get(f'/api/sessions/{sid}/history').json()), 2)

    def test_uncertain_commit_failure_preserves_a_durable_result(self):
        jid = self.queue.enqueue('medium', 'ingest', {'path': '/tmp/test.pdf'})
        job = self.queue.claim(['medium'])
        with db_session() as db:
            self.queue.persist_result(db, job, {'status': 'indexed', 'generation_id': 'published'})
        self.queue.fail(job, 'Connection lost after commit', retry=False)
        self.assertEqual(self.queue.status(jid)['status'], 'succeeded')
        self.assertIsNone(self.queue.status(jid)['error'])

    def test_legacy_pending_upload_keeps_original_document_identity(self):
        legacy = {'id': 'old-redis-job', 'kind': 'ingest',
                  'payload': {'path': '/data/incoming/admin/1750000000000_guide_v2.docx', 'origin': 'admin'}}
        self.redis.brpop.return_value = ('queue:medium', json.dumps(legacy))
        job = self.queue.pop(['medium'])
        self.assertEqual(job['id'], legacy['id'])
        self.assertEqual(job['payload']['source_name'], 'guide_v2.docx')

    def test_chat_is_async_and_idempotent_and_does_not_duplicate_history(self):
        sid = self.session()
        payload = {'session_id': sid, 'message': 'Which VLAN?',
                   'request_id': '78dfcc2d-5f3e-4b9a-8ba8-821430ce75f6'}
        first = self.client.post('/api/chat', json=payload)
        second = self.client.post('/api/chat', json=payload)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json(), second.json())
        jid = first.json()['job_id']
        job = self.queue.claim(['high'])
        rag = Mock()
        rag.answer.return_value = {'answer': 'VLAN 120 [S1]', 'sources': [{'id': 'S1', 'source': 'guide.pdf', 'page': 2}]}
        result = dispatch(job, self.queue, lambda: rag)
        self.assertEqual(rag.answer.call_args.args[1], [])
        # A crash after message persistence but before acknowledgment reuses the saved answer.
        dispatch(job, self.queue, lambda: self.fail('Answer regenerated after persistence'))
        self.queue.complete(job, result)
        history = self.client.get(f'/api/sessions/{sid}/history').json()
        self.assertEqual(len(history), 2)
        self.assertEqual(history[-1]['sources'][0]['page'], 2)
        self.assertEqual(self.client.get(f'/api/jobs/{jid}').json()['status'], 'succeeded')

    def test_pending_conversation_is_serialized_and_can_be_rediscovered(self):
        sid = self.session()
        response = self.client.post('/api/chat', json={'session_id': sid, 'message': 'First question'})
        jid = response.json()['job_id']
        duplicate = self.client.post('/api/chat', json={'session_id': sid, 'message': 'Second question'})
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json()['detail']['job_id'], jid)
        self.assertEqual(self.client.get('/api/sessions').json()[0]['pending_job_id'], jid)
        self.assertEqual(self.client.get(f'/api/jobs?session_id={sid}').json()[0]['job_id'], jid)

    def test_request_id_cannot_be_reused_for_a_different_message_or_session(self):
        sid = self.session()
        payload = {'session_id': sid, 'message': 'First question',
                   'request_id': 'b4a7556f-a6be-466a-8490-a3b6c7239945'}
        original = self.client.post('/api/chat', json=payload)
        self.assertEqual(original.status_code, 202)
        changed_message = self.client.post('/api/chat', json={**payload, 'message': 'Different question'})
        self.assertEqual(changed_message.status_code, 409)
        changed_session = self.client.post('/api/chat', json={**payload, 'session_id': self.session()})
        self.assertEqual(changed_session.status_code, 409)
        self.assertEqual(self.client.post('/api/chat', json=payload).json(), original.json())
        with db_session() as db:
            self.assertEqual(len(db.scalars(select(JobRecord)).all()), 1)

    def test_database_outage_returns_structured_health_failure(self):
        with patch('app.health.db_session', side_effect=RuntimeError('database unavailable')):
            response = self.client.get('/api/health')
            self.assertFalse(worker_ready(queues=['high']))
        self.assertEqual(response.status_code, 503)
        checks = response.json()['checks']
        self.assertEqual(checks['postgres'], 'unavailable')
        self.assertEqual(checks['chat_worker'], 'unavailable')
        self.assertEqual(checks['ingestion_worker'], 'unavailable')
        self.assertEqual(checks['cleanup_worker'], 'unavailable')

    def test_session_and_job_ownership_are_enforced(self):
        sid = self.session()
        jid = self.queue.enqueue('high', 'chat', {'session_id': sid}, user_id=self.user_id)
        self.client.cookies.set('rag_auth', 'other-token')
        self.assertEqual(self.client.get(f'/api/sessions/{sid}/history').status_code, 404)
        self.assertEqual(self.client.post(f'/api/sessions/{sid}/resume').status_code, 404)
        self.assertEqual(self.client.get(f'/api/jobs/{jid}').status_code, 404)
        self.assertEqual(self.client.get('/api/admin/documents').status_code, 403)

    def test_expired_session_can_resume_without_losing_history(self):
        sid = self.session()
        with db_session() as db:
            db.get(ChatSession, sid).last_activity = now_utc() - timedelta(hours=1)
        self.assertEqual(self.client.post('/api/chat', json={'session_id': sid, 'message': 'Question'}).status_code, 440)
        self.assertEqual(self.client.post(f'/api/sessions/{sid}/resume').status_code, 200)
        self.assertEqual(self.client.post('/api/chat', json={'session_id': sid, 'message': 'Question'}).status_code, 202)

    def test_upload_preserves_identity_and_uses_distinct_storage_paths(self):
        path = Path(self.temp.name) / 'fixture.docx'
        doc = Document(); doc.add_paragraph('Management VLAN 120'); doc.save(path)
        replies = []
        for version in [1, 2]:
            with path.open('rb') as file:
                response = self.client.post('/api/admin/documents', files={'file': (f'guide_v{version}.docx', file)})
            self.assertEqual(response.status_code, 202)
            replies.append(response.json())
        with db_session() as db:
            jobs = [db.get(JobRecord, row['job_id']) for row in replies]
            self.assertEqual(jobs[0].payload['topic_key'], jobs[1].payload['topic_key'])
            self.assertEqual(jobs[1].payload['source_name'], 'guide_v2.docx')
            self.assertNotEqual(Path(jobs[0].payload['path']).parent, Path(jobs[1].payload['path']).parent)
        self.assertEqual(self.client.post('/api/admin/documents', files={'file': ('bad.pdf', b'not a pdf')}).status_code, 400)

    def test_login_limit_still_works_without_redis(self):
        for _ in range(3):
            queueing.consume_rate_limit('attacker', 'login', 3)
        with self.assertRaises(RateLimitExceeded):
            queueing.consume_rate_limit('attacker', 'login', 3)
        queueing.consume_rate_limit('another-client', 'login', 3)

    def test_document_removal_cancels_inflight_replacement(self):
        with db_session() as db:
            row = DocumentRegistry(topic_key='admin/guide', source='guide_v1.pdf',
                version_label='v1', version_rank=1, sha256='digest', generation_id='old', status='active')
            db.add(row); db.flush(); document_id = row.id
        jid = self.queue.enqueue('medium', 'ingest', {'path': '/tmp/guide.pdf', 'topic_key': 'admin/guide'}, user_id=self.user_id)
        job = self.queue.claim(['medium'])
        with patch('app.rag.RagService', side_effect=RuntimeError('offline')):
            response = self.client.delete(f'/api/admin/documents/{document_id}')
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.queue.status(jid)['status'], 'failed')
        with self.assertRaises(LeaseLostError):
            with db_session() as db:
                self.queue.assert_lease(db, job)
        with db_session() as db:
            self.assertEqual(db.get(DocumentRegistry, document_id).status, 'deleted')

    def test_cleanup_does_not_remove_inflight_conversation(self):
        sid = self.session()
        with db_session() as db:
            db.get(ChatSession, sid).last_activity = now_utc() - timedelta(days=8)
            db.add(ChatMessage(session_id=sid, user_id=self.user_id, role='user', content='Pending',
                               created_at=now_utc() - timedelta(days=8)))
        self.queue.enqueue('high', 'chat', {'session_id': sid}, user_id=self.user_id)
        cleanup_once()
        with db_session() as db:
            self.assertIsNotNone(db.get(ChatSession, sid))
            self.assertEqual(len(db.scalars(select(ChatMessage).where(ChatMessage.session_id == sid)).all()), 1)

    def test_existing_schema_receives_additive_migrations(self):
        with self.engine.begin() as connection:
            connection.execute(text('DROP TABLE chat_messages'))
            connection.execute(text('DROP TABLE document_registry'))
            connection.execute(text('CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, session_id VARCHAR(36), user_id INTEGER, role VARCHAR(20), content TEXT, created_at DATETIME)'))
            connection.execute(text('CREATE TABLE document_registry (id INTEGER PRIMARY KEY, topic_key VARCHAR(255), source TEXT, version_label VARCHAR(80), version_rank INTEGER, sha256 VARCHAR(64), generation_id VARCHAR(64), status VARCHAR(30), indexed_at DATETIME)'))
        database.init_db()
        self.assertTrue({'sources', 'job_id'}.issubset({item['name'] for item in inspect(self.engine).get_columns('chat_messages')}))
        self.assertTrue({'storage_path', 'origin', 'chunk_count'}.issubset({item['name'] for item in inspect(self.engine).get_columns('document_registry')}))

    def test_health_checks_this_process_instead_of_another_fresh_worker(self):
        marker = Path(self.temp.name) / 'worker.json'
        with patch('app.worker.Path', return_value=marker), patch('app.health.Path', return_value=marker):
            touch_worker('old-worker', queues=['high'])
            marker.unlink()
            self.assertTrue(worker_ready(queues=['high']))
            self.assertFalse(local_worker_ready('worker', ['high']))
            touch_worker('current-worker', queues=['medium'])
            self.assertTrue(local_worker_ready('worker', ['medium']))
            self.assertFalse(local_worker_ready('worker', ['high']))

    def test_graceful_retirement_removes_only_own_heartbeat(self):
        marker = Path(self.temp.name) / 'worker.json'
        with patch('app.worker.Path', return_value=marker), patch('app.health.Path', return_value=marker):
            touch_worker('other-worker', queues=['medium'])
            touch_worker('current-worker', queues=['high'])
            retire_worker('current-worker')
            self.assertFalse(marker.exists())
            self.assertTrue(worker_ready(queues=['medium']))
            self.assertFalse(worker_ready(queues=['high']))


if __name__ == '__main__':
    unittest.main()
