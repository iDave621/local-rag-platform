"""Dependency readiness without starting inference or downloading models."""
import shutil
import json
from datetime import timedelta
from pathlib import Path

import httpx
from sqlalchemy import select, text

from .config import get_settings
from .db import WorkerHeartbeat, db_session, now_utc
from .queueing import redis_client


def worker_ready(component='worker', queues=None, worker_id=None):
    cut = now_utc() - timedelta(seconds=get_settings().worker_health_timeout_seconds)
    try:
        with db_session() as db:
            query = select(WorkerHeartbeat).where(
                WorkerHeartbeat.component == component, WorkerHeartbeat.last_seen >= cut)
            if worker_id:
                query = query.where(WorkerHeartbeat.id == worker_id)
            rows = db.scalars(query).all()
            if queues:
                available = {queue for row in rows for queue in row.queues}
                return set(queues).issubset(available)
            return bool(rows)
    except Exception:
        return False


def local_worker_ready(component, queues=None):
    try:
        marker = json.loads(Path(f'/tmp/rag-worker-{component}.json').read_text())
        return marker.get('component') == component and worker_ready(component, queues, marker['id'])
    except (OSError, ValueError, KeyError):
        return False


def dependency_checks(include_workers=False):
    settings = get_settings()
    checks = {}
    try:
        with db_session() as db:
            db.execute(text('SELECT 1'))
        checks['postgres'] = 'ok'
    except Exception:
        checks['postgres'] = 'unavailable'
    try:
        redis_client().ping()
        checks['redis'] = 'ok'
    except Exception:
        checks['redis'] = 'unavailable'
    try:
        scheme = 'https' if settings.chroma_ssl else 'http'
        headers = {'X-Chroma-Token': settings.chroma_auth_token} if settings.chroma_auth_token else {}
        with httpx.Client(timeout=3, verify=settings.chroma_ca_file or True, trust_env=False) as client:
            response = client.get(f'{scheme}://{settings.chroma_host}:{settings.chroma_port}/api/v2/heartbeat', headers=headers)
            response.raise_for_status()
        checks['chroma'] = 'ok'
    except Exception:
        checks['chroma'] = 'unavailable'
    try:
        with httpx.Client(timeout=3, trust_env=False) as client:
            response = client.get(settings.ollama_base_url.rstrip('/') + '/api/tags')
            response.raise_for_status()
        available = {model.get('name', '') for model in response.json().get('models', [])}
        required = [settings.ollama_chat_model, settings.ollama_embed_model]
        if settings.vision_enabled:
            required.append(settings.ollama_vision_model)
        missing = [name for name in required if name not in available and name + ':latest' not in available]
        checks['ollama'] = 'missing models: ' + ', '.join(missing) if missing else 'ok'
    except Exception:
        checks['ollama'] = 'unavailable'
    if settings.ocr_enabled:
        checks['ocr'] = 'ok' if shutil.which('tesseract') else 'unavailable'
    if include_workers:
        checks['chat_worker'] = 'ok' if worker_ready(queues=['high']) else 'unavailable'
        checks['ingestion_worker'] = 'ok' if worker_ready(queues=['medium']) else 'unavailable'
        checks['cleanup_worker'] = 'ok' if worker_ready('cleanup') else 'unavailable'
        if settings.literature_site_url:
            checks['sync_worker'] = 'ok' if worker_ready('sync') else 'unavailable'
    return checks
