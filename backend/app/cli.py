import argparse
import getpass
import json
from pathlib import Path

from sqlalchemy import select

from .config import get_settings
from .db import JobRecord, User, db_session, init_db, now_utc
from .health import dependency_checks, local_worker_ready
from .queueing import JobQueue
from .security import hash_password


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init-db')
    create = commands.add_parser('create-user')
    create.add_argument('--username')
    create.add_argument('--role', choices=['technician', 'admin'], default='technician')
    health = commands.add_parser('health')
    health.add_argument('--component', choices=['all', 'api', 'worker', 'cleanup', 'sync', 'chroma', 'ollama'], default='all')
    health.add_argument('--queues', default='')
    commands.add_parser('repair-documents')
    ingest = commands.add_parser('ingest')
    ingest.add_argument('path')
    ingest.add_argument('--source-name')
    retry = commands.add_parser('retry-job')
    retry.add_argument('job_id')
    args = parser.parse_args()

    if args.command == 'health':
        checks = dependency_checks(include_workers=args.component == 'api')
        if args.component in {'chroma', 'ollama'}:
            checks = {args.component: checks[args.component]}
        elif args.component in {'worker', 'cleanup', 'sync'}:
            queues = [value.strip() for value in args.queues.split(',') if value.strip()]
            checks[args.component] = 'ok' if local_worker_ready(args.component, queues or None) else 'unavailable'
        print(json.dumps(checks))
        raise SystemExit(0 if all(value == 'ok' for value in checks.values()) else 1)

    init_db()
    if args.command == 'init-db':
        print('Database initialized and additive migrations applied')
        return
    if args.command == 'repair-documents':
        from .ingestion import repair_legacy_documents
        print(json.dumps(repair_legacy_documents(), ensure_ascii=False))
        return
    if args.command == 'ingest':
        from .ingestion import ingest_path
        path = Path(args.path).resolve()
        if not path.is_relative_to(get_settings().data_path.resolve()):
            raise SystemExit('Place documents under DATA_ROOT before importing')
        print(json.dumps(ingest_path(str(path), 'local', source_name=args.source_name or path.name), ensure_ascii=False))
        return
    if args.command == 'retry-job':
        with db_session() as db:
            row = db.get(JobRecord, args.job_id)
            if not row or row.status != 'failed':
                raise SystemExit('Only failed jobs can be retried')
            row.status = 'queued'
            row.attempts = 0
            row.error = None
            row.available_at = now_utc()
            row.updated_at = now_utc()
            queue_name = row.queue
        JobQueue().notify(queue_name, args.job_id)
        print('Job queued for retry')
        return

    username = args.username or input('Username: ')
    password = getpass.getpass('Password: ')
    confirmation = getpass.getpass('Confirm password: ')
    if len(username) < 3 or len(username) > 80:
        raise SystemExit('Username must contain 3 to 80 characters')
    if password != confirmation or not 12 <= len(password) <= 256:
        raise SystemExit('Passwords must match and contain 12 to 256 characters')
    with db_session() as db:
        if db.scalar(select(User).where(User.username == username)):
            raise SystemExit('User exists')
        db.add(User(username=username, password_hash=hash_password(password), role=args.role, active=True))
    print('Created', username, args.role)


if __name__ == '__main__':
    main()
