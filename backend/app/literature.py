"""Optional internal-site sync; enqueueing never means a document was indexed."""
import hashlib
import json
import os
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select

from .config import get_settings
from .db import db_session, DocumentRegistry
from .queueing import JobQueue
from .versioning import identify_version

SUPPORTED = {'.pdf', '.doc', '.docx', '.txt', '.md', '.png', '.jpg', '.jpeg', '.webp'}


@dataclass
class Remote:
    url: str
    filename: str
    topic: str
    rank: int
    label: str


def canonical(url):
    parsed = urlparse(url)
    # Queries may be the entire download identity or contain a document version.
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, parsed.query, ''))


def filename(url, text=''):
    name = unquote(PurePosixPath(urlparse(url).path).name)
    if Path(name).suffix.lower() not in SUPPORTED:
        name = text.strip()
    name = PurePosixPath(name.replace('\\', '/')).name
    if '\x00' in name or name in {'.', '..'} or len(name) > 200:
        return ''
    return name if Path(name).suffix.lower() in SUPPORTED else ''


class LiteratureSync:
    def __init__(self):
        self.s = get_settings()
        self.base = self.s.literature_site_url
        self.root = self.s.data_path / 'literature_cache'
        self.root.mkdir(parents=True, exist_ok=True)
        self.state = self.root / 'sync-state.json'
        self.q = JobQueue()

    def _same_origin(self, url):
        base, parsed = urlparse(self.base), urlparse(url)
        return parsed.scheme in {'http', 'https'} and (
            parsed.scheme.casefold(), parsed.netloc.casefold()
        ) == (base.scheme.casefold(), base.netloc.casefold())

    def _get(self, client, url):
        # Follow only redirects staying on the configured internal origin.
        for _ in range(6):
            if not self._same_origin(url):
                raise ValueError('Literature link redirects outside the configured internal site')
            with client.stream('GET', url) as response:
                if response.is_redirect:
                    location = response.headers.get('location')
                    if not location:
                        raise ValueError('Literature redirect has no destination')
                    url = canonical(urljoin(url, location))
                    continue
                response.raise_for_status()
                blocks, total = [], 0
                for block in response.iter_bytes():
                    total += len(block)
                    if total > self.s.admin_upload_max_bytes:
                        raise ValueError('Literature response exceeds the configured upload size limit')
                    blocks.append(block)
                return httpx.Response(
                    response.status_code, headers=response.headers,
                    content=b''.join(blocks), request=response.request,
                )
        raise ValueError('Too many literature redirects')

    def discover(self):
        if not self.base:
            return []
        queue, seen, files, pages = deque([self.base]), set(), {}, 0
        with httpx.Client(
            timeout=self.s.literature_request_timeout_seconds,
            verify=self.s.literature_verify_tls, headers=self.s.literature_headers,
            follow_redirects=False,
        ) as client:
            while queue and pages < self.s.literature_crawl_max_pages:
                url = canonical(queue.popleft())
                if url in seen or not self._same_origin(url):
                    continue
                seen.add(url)
                pages += 1  # Failed pages also count towards the crawl limit.
                try:
                    response = self._get(client, url)
                except (httpx.HTTPError, ValueError):
                    continue
                if 'html' not in response.headers.get('content-type', '').lower():
                    continue
                soup = BeautifulSoup(response.text, 'html.parser')
                for tag, attribute in [('a', 'href'), ('img', 'src')]:
                    for node in soup.find_all(tag):
                        raw = str(node.get(attribute) or '')
                        if not raw or raw.startswith(('data:', 'javascript:', 'mailto:', '#')):
                            continue
                        absolute = canonical(urljoin(str(response.url), raw))
                        if not self._same_origin(absolute):
                            continue
                        name = filename(
                            absolute, node.get_text(' ', strip=True)
                            if tag == 'a' else str(node.get('alt') or ''),
                        )
                        if name:
                            parent = str(PurePosixPath(unquote(urlparse(absolute).path)).parent)
                            identity = identify_version(name, parent)
                            # Retain the remote directory and authority instead of
                            # deriving identity again from a local cache filename.
                            authority = urlparse(absolute).netloc.casefold()
                            topic = f'internal_site/{authority}/{identity.topic_key}'
                            if len(topic) > 255:
                                topic = f'internal_site/{authority}/{hashlib.sha256(identity.topic_key.encode()).hexdigest()}'
                            files[absolute] = Remote(
                                absolute, name, topic, identity.version_rank, identity.version_label,
                            )
                        elif tag == 'a' and absolute not in seen:
                            queue.append(absolute)
        latest = {}
        for item in files.values():
            if item.topic not in latest or (item.rank, item.url) > (latest[item.topic].rank, latest[item.topic].url):
                latest[item.topic] = item
        return list(latest.values())

    def _registry_indexed(self, item, digest):
        with db_session() as db:
            return db.scalar(select(DocumentRegistry.id).where(
                DocumentRegistry.topic_key == item.topic,
                DocumentRegistry.status == 'active',
                ((DocumentRegistry.version_rank > item.rank) |
                 ((DocumentRegistry.version_rank == item.rank) & (DocumentRegistry.sha256 == digest))),
            )) is not None

    def run_once(self):
        if not self.base:
            return {'latest_files': 0, 'queued': 0, 'indexed': 0, 'errors': []}
        items = self.discover()
        if not items:
            raise RuntimeError('No literature files discovered; keeping current KB unchanged')
        try:
            previous = json.loads(self.state.read_text(encoding='utf-8')).get('files', {})
        except (OSError, ValueError, AttributeError):
            previous = {}
        state = dict(previous)
        queued, indexed, errors = 0, 0, []
        with httpx.Client(
            timeout=self.s.literature_request_timeout_seconds,
            verify=self.s.literature_verify_tls, headers=self.s.literature_headers,
            follow_redirects=False,
        ) as client:
            for item in items:
                try:
                    response = self._get(client, item.url)
                    data = response.content
                    if len(data) > self.s.admin_upload_max_bytes:
                        raise ValueError('Literature document exceeds the configured upload size limit')
                    digest = hashlib.sha256(data).hexdigest()
                    prefix = hashlib.sha256(item.topic.encode()).hexdigest()[:16]
                    # Immutable cache paths prevent a newer download overwriting
                    # the file an older queued job is about to read.
                    target = self.root / f'{prefix}_{digest[:12]}_{item.filename}'
                    old = previous.get(item.topic, {})
                    same = old.get('sha256') == digest and old.get('rank') == item.rank
                    job = self.q.status(old['job_id']) if same and old.get('job_id') else None
                    complete = False
                    if job and job.get('status') == 'succeeded':
                        complete = (
                            (job.get('result') or {}).get('status') in {'indexed', 'unchanged', 'skipped_older'}
                            and self._registry_indexed(item, digest)
                        )
                    elif same and old.get('status') == 'indexed':
                        # Reconfirm the registry, including after an administrator
                        # removes a previously synchronized document.
                        complete = self._registry_indexed(item, digest)
                    elif same and not job:
                        complete = self._registry_indexed(item, digest)
                    attempt = int(old.get('attempt', 0)) if same else 0
                    record = {
                        'url': item.url, 'filename': item.filename, 'version': item.label,
                        'rank': item.rank, 'sha256': digest, 'local': str(target),
                        'attempt': attempt,
                    }
                    if complete:
                        record.update(status='indexed', job_id=old.get('job_id'))
                        indexed += 1
                    elif job and job.get('status') in {'queued', 'processing'}:
                        record.update(status=job['status'], job_id=old['job_id'])
                    else:
                        if same and old:
                            attempt += 1
                        if not target.exists():
                            temporary = target.with_name(target.name + f'.{uuid.uuid4().hex}.part')
                            temporary.write_bytes(data)
                            os.replace(temporary, target)
                        job_id = self.q.enqueue(
                            'low', 'ingest',
                            {
                                'path': str(target), 'origin': 'internal_site',
                                'source_name': item.filename, 'topic_key': item.topic,
                                'version_rank': item.rank, 'version_label': item.label,
                            },
                            idempotency_key=f'literature:{prefix}:{digest}:{item.rank}:{attempt}',
                        )
                        record.update(status='queued', job_id=job_id, attempt=attempt)
                        queued += 1
                    state[item.topic] = record
                except Exception as error:
                    errors.append({'topic': item.topic, 'error': str(error)})
        temporary_state = self.state.with_name(self.state.name + f'.{uuid.uuid4().hex}.tmp')
        temporary_state.write_text(
            json.dumps({'files': state}, ensure_ascii=False, indent=2), encoding='utf-8',
        )
        os.replace(temporary_state, self.state)
        return {'latest_files': len(items), 'queued': queued, 'indexed': indexed, 'errors': errors}
