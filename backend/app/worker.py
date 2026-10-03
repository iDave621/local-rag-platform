import logging
import json
import os
import signal
import threading
import time
import uuid
from pathlib import Path

from sqlalchemy import select

from .config import get_settings
from .db import ChatMessage, WorkerHeartbeat, db_session, now_utc
from .queueing import JobQueue, LeaseLostError
from .rag import RagService
from .ingestion import ingest_path

log = logging.getLogger(__name__)


def touch_worker(worker_id, component='worker', queues=None):
    with db_session() as db:
        row = db.get(WorkerHeartbeat, worker_id)
        if row:
            row.last_seen = now_utc()
        else:
            db.add(WorkerHeartbeat(id=worker_id, component=component,
                                   queues=queues or [], last_seen=now_utc()))
    marker = Path(f'/tmp/rag-worker-{component}.json')
    contents = json.dumps({'id': worker_id, 'component': component})
    if not marker.exists() or marker.read_text() != contents:
        marker.write_text(contents, encoding='utf-8')


def retire_worker(worker_id, component='worker'):
    try:
        with db_session() as db:
            row = db.get(WorkerHeartbeat, worker_id)
            if row:
                db.delete(row)
    except Exception:
        log.warning('Worker heartbeat retirement deferred')
    Path(f'/tmp/rag-worker-{component}.json').unlink(missing_ok=True)


def dispatch(job, queue, rag_factory=RagService):
    payload = job['payload']
    if job['kind'] == 'chat':
        with db_session() as db:
            previous = db.scalar(select(ChatMessage).where(
                ChatMessage.job_id == job['id'], ChatMessage.role == 'assistant'))
            if previous:
                result = {'answer': previous.content, 'sources': previous.sources}
                queue.persist_result(db, job, result)
                return result
            query = select(ChatMessage).where(
                ChatMessage.session_id == payload['session_id'],
                ChatMessage.user_id == payload['user_id'])
            cutoff = payload.get('history_before_id')
            if cutoff:
                query = query.where(ChatMessage.id < cutoff)
            rows = db.scalars(query.order_by(ChatMessage.id.desc()).limit(
                get_settings().max_history_messages)).all()
            history = [{'role': row.role, 'content': row.content} for row in reversed(rows)]
        result = rag_factory().answer(payload['message'], history)
        with db_session() as db:
            queue.assert_lease(db, job)
            previous = db.scalar(select(ChatMessage).where(
                ChatMessage.job_id == job['id'], ChatMessage.role == 'assistant'))
            if not previous:
                db.add(ChatMessage(session_id=payload['session_id'],
                                   user_id=payload['user_id'], role='assistant',
                                   content=result['answer'], sources=result.get('sources', []),
                                   job_id=job['id']))
            queue.persist_result(db, job, result)
        return result
    if job['kind'] == 'ingest':
        identity = {name: payload[name] for name in
                    ('source_name', 'topic_key', 'version_rank', 'version_label') if name in payload}
        return ingest_path(payload['path'], payload.get('origin', 'admin'),
                           publication_guard=lambda db: queue.assert_lease(db, job),
                           persist_result=lambda db, result: queue.persist_result(db, job, result), **identity)
    raise ValueError('Unknown job type')


def main():
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    queue = JobQueue()
    queues = [name.strip() for name in settings.worker_queues.split(',') if name.strip()]
    worker_id = f'worker-{os.getpid()}-{uuid.uuid4().hex[:12]}'
    active = [None]
    stop = threading.Event()
    shutdown = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: shutdown.set())
    signal.signal(signal.SIGINT, lambda *_: shutdown.set())

    def heartbeat():
        while not stop.is_set():
            try:
                touch_worker(worker_id, queues=queues)
                if active[0]:
                    queue.heartbeat(active[0])
            except Exception:
                log.exception('Heartbeat failed')
            stop.wait(settings.job_heartbeat_seconds)

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    cached_rag = [None]

    def rag_factory():
        if cached_rag[0] is None:
            cached_rag[0] = RagService()
        return cached_rag[0]

    try:
        while not shutdown.is_set():
            try:
                job = queue.pop(queues, timeout=5)
                if not job:
                    continue
                active[0] = job
                try:
                    queue.complete(job, dispatch(job, queue, rag_factory))
                except LeaseLostError:
                    log.warning('Discarding reassigned job %s', job['id'])
                except Exception as error:
                    log.exception('Job %s failed', job['id'])
                    try:
                        queue.fail(job, error, retry=not isinstance(error, (ValueError, FileNotFoundError)))
                    except LeaseLostError:
                        pass
                finally:
                    active[0] = None
            except Exception:
                log.exception('Worker dependency temporarily unavailable')
                shutdown.wait(2)
    finally:
        stop.set()
        thread.join(timeout=2)
        retire_worker(worker_id)


if __name__ == '__main__':
    main()
