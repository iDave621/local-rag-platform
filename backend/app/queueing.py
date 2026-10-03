"""PostgreSQL is the durable job ledger; Redis is only a wake-up signal."""
import asyncio
import hashlib
import json
import re
import time
import uuid
from datetime import timedelta
from pathlib import Path

import redis
from sqlalchemy import case, select

from .config import get_settings
from .db import JobRecord, RateLimitWindow, db_session, now_utc

s = get_settings()


class RateLimitExceeded(RuntimeError):
    pass


class LeaseLostError(RuntimeError):
    pass


def redis_client():
    return redis.Redis.from_url(s.redis_url, decode_responses=True,
                                socket_connect_timeout=2, socket_timeout=6)


class JobQueue:
    def __init__(self):
        self.r = redis_client()

    def enqueue(self, queue, kind, payload, user_id=None, idempotency_key=None,
                db=None, job_id=None):
        if queue not in {'high', 'medium', 'low'}:
            raise ValueError('Invalid queue')
        if db is None:
            with db_session() as session:
                jid = self.enqueue(queue, kind, payload, user_id, idempotency_key,
                                   session, job_id)
            self.notify(queue, jid)
            return jid
        if idempotency_key:
            previous = db.scalar(select(JobRecord).where(JobRecord.idempotency_key == idempotency_key))
            if previous:
                return previous.id
        jid = job_id or str(uuid.uuid4())
        db.add(JobRecord(id=jid, queue=queue, kind=kind, payload=payload,
                         user_id=user_id, session_id=payload.get('session_id'),
                         idempotency_key=idempotency_key, max_attempts=s.job_max_attempts))
        db.flush()
        return jid

    def notify(self, queue, jid):
        try:
            self.r.lpush(f'queue:{queue}', jid)
        except redis.RedisError:
            # The committed SQL job will still be discovered by workers.
            pass

    def recover_expired(self, db):
        now = now_utc()
        rows = db.scalars(select(JobRecord).where(
            JobRecord.status == 'processing', JobRecord.lease_until < now
        ).with_for_update(skip_locked=True)).all()
        for row in rows:
            if row.result is not None:
                row.status = 'succeeded'
                row.error = None
            else:
                row.status = 'queued' if row.attempts < row.max_attempts else 'failed'
                row.error = 'Worker interrupted; retrying' if row.status == 'queued' else 'Worker interrupted; retry limit reached'
            row.claim_token = None
            row.lease_until = None
            row.available_at = now
            row.updated_at = now

    def claim(self, queues):
        with db_session() as db:
            self.recover_expired(db)
            rank = case({name: i for i, name in enumerate(queues)}, value=JobRecord.queue, else_=99)
            row = db.scalar(select(JobRecord).where(
                JobRecord.queue.in_(queues), JobRecord.status == 'queued',
                JobRecord.available_at <= now_utc()
            ).order_by(rank, JobRecord.created_at).with_for_update(skip_locked=True).limit(1))
            if not row:
                return None
            row.status = 'processing'
            row.attempts += 1
            row.claim_token = str(uuid.uuid4())
            row.lease_until = now_utc() + timedelta(seconds=s.job_lease_seconds)
            row.updated_at = now_utc()
            return {'id': row.id, 'kind': row.kind, 'payload': row.payload,
                    'queue': row.queue, 'claim_token': row.claim_token,
                    'attempts': row.attempts}

    def pop(self, queues, timeout=5):
        job = self.claim(queues)
        if job:
            return job
        try:
            signal = self.r.brpop([f'queue:{name}' for name in queues], timeout=timeout)
            # Import pending jobs from the previous Redis-only release.
            if signal and signal[1].startswith('{'):
                old = json.loads(signal[1])
                if not self.status(old['id']):
                    payload = old['payload']
                    if old['kind'] == 'ingest' and payload.get('origin', 'admin') == 'admin' and not payload.get('source_name'):
                        payload['source_name'] = re.sub(r'^\d{10,}_', '', Path(payload.get('path', '')).name)
                    self.enqueue(signal[0].split(':', 1)[1], old['kind'],
                                 payload, user_id=payload.get('user_id'),
                                 job_id=old['id'])
        except (redis.RedisError, ValueError, KeyError):
            time.sleep(min(timeout, 2))
        return self.claim(queues)

    def assert_lease(self, db, job):
        row = db.scalar(select(JobRecord).where(JobRecord.id == job['id']).with_for_update())
        if not row or row.status != 'processing' or row.claim_token != job['claim_token']:
            raise LeaseLostError('Job lease was reassigned')
        return row

    def heartbeat(self, job):
        with db_session() as db:
            row = self.assert_lease(db, job)
            row.lease_until = now_utc() + timedelta(seconds=s.job_lease_seconds)
            row.updated_at = now_utc()

    def persist_result(self, db, job, result):
        # Call in the transaction that saves the answer or publishes its index.
        # Recovery can acknowledge this result even on the final allowed attempt.
        row = self.assert_lease(db, job)
        row.result = result
        row.updated_at = now_utc()

    def complete(self, job, result):
        with db_session() as db:
            row = self.assert_lease(db, job)
            row.status = 'succeeded'
            row.result = result
            row.error = None
            row.claim_token = None
            row.lease_until = None
            row.updated_at = now_utc()

    def fail(self, job, error, retry=True):
        with db_session() as db:
            row = self.assert_lease(db, job)
            row.status = 'succeeded' if row.result is not None else (
                'queued' if retry and row.attempts < row.max_attempts else 'failed')
            row.error = None if row.status == 'succeeded' else str(error)[:2000]
            row.claim_token = None
            row.lease_until = None
            row.available_at = now_utc() + timedelta(seconds=min(30, 2 ** row.attempts))
            row.updated_at = now_utc()

    def status(self, jid):
        with db_session() as db:
            row = db.get(JobRecord, jid)
            if not row:
                return None
            return {'job_id': row.id, 'status': row.status, 'result': row.result,
                    'error': row.error, 'user_id': row.user_id, 'kind': row.kind,
                    'filename': row.payload.get('source_name') or Path(row.payload.get('path', '')).name,
                    'origin': row.payload.get('origin'),
                    'session_id': row.session_id, 'attempts': row.attempts,
                    'created_at': row.created_at.isoformat(),
                    'updated_at': row.updated_at.isoformat()}

    async def wait(self, jid, timeout=None):
        deadline = time.monotonic() + (timeout or s.chat_result_timeout_seconds)
        while time.monotonic() < deadline:
            row = await asyncio.to_thread(self.status, jid)
            if row and row['status'] in {'succeeded', 'failed'}:
                return {'ok': row['status'] == 'succeeded',
                        'data': row['result'], 'error': row['error']}
            await asyncio.sleep(.5)
        raise TimeoutError


def consume_rate_limit(uid, action, limit):
    bucket = int(time.time() // 60)
    key = hashlib.sha256(f'{uid}:{action}:{bucket}'.encode()).hexdigest()
    try:
        count = redis_client().eval(
            "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],70) end; return n",
            1, f'rl:{key}')
    except redis.RedisError:
        # Keep login protection effective even while Redis is unavailable.
        with db_session() as db:
            dialect = db.bind.dialect.name
            if dialect == 'postgresql':
                from sqlalchemy.dialects.postgresql import insert
            else:
                from sqlalchemy.dialects.sqlite import insert
            statement = insert(RateLimitWindow).values(
                key=key, count=1, expires_at=now_utc() + timedelta(seconds=70))
            statement = statement.on_conflict_do_update(
                index_elements=[RateLimitWindow.key],
                set_={'count': RateLimitWindow.count + 1}).returning(RateLimitWindow.count)
            count = db.scalar(statement)
    if count > limit:
        raise RateLimitExceeded('Rate limit exceeded')
