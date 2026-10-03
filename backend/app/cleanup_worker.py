import logging
import signal
import threading
import time
import uuid
from datetime import timedelta

from sqlalchemy import delete, select

from .config import get_settings
from .db import (AuthSession, ChatMessage, ChatSession, JobRecord, RateLimitWindow,
                 WorkerHeartbeat, db_session, now_utc)
from .worker import touch_worker, retire_worker


def cleanup_once():
    settings = get_settings()
    now = now_utc()
    cut = now - timedelta(days=settings.chat_retention_days)
    inactive = now - timedelta(seconds=settings.session_timeout_seconds)
    with db_session() as db:
        pending_sessions = select(JobRecord.session_id).where(
            JobRecord.session_id.is_not(None), JobRecord.status.in_(['queued', 'processing']))
        db.execute(delete(ChatMessage).where(ChatMessage.created_at < cut,
                                             ChatMessage.session_id.not_in(pending_sessions)))
        db.execute(delete(ChatSession).where(ChatSession.last_activity < cut,
                                             ChatSession.id.not_in(pending_sessions)))
        db.execute(delete(AuthSession).where(AuthSession.expires_at < now))
        db.execute(delete(RateLimitWindow).where(RateLimitWindow.expires_at < now))
        db.execute(delete(JobRecord).where(
            JobRecord.status.in_(['succeeded', 'failed']),
            JobRecord.updated_at < now - timedelta(days=settings.job_retention_days)))
        db.execute(delete(WorkerHeartbeat).where(
            WorkerHeartbeat.last_seen < now - timedelta(days=1)))
        for row in db.scalars(select(ChatSession).where(
            ChatSession.active.is_(True), ChatSession.last_activity < inactive)):
            row.active = False


def main():
    logging.basicConfig(level=logging.INFO)
    worker_id = 'cleanup-' + uuid.uuid4().hex[:12]
    shutdown = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: shutdown.set())
    signal.signal(signal.SIGINT, lambda *_: shutdown.set())
    try:
        while not shutdown.is_set():
            try:
                cleanup_once()
                touch_worker(worker_id, component='cleanup')
            except Exception:
                logging.exception('Cleanup dependency temporarily unavailable')
            shutdown.wait(60)
    finally:
        retire_worker(worker_id, 'cleanup')


if __name__ == '__main__':
    main()
