"""Rotate service-owned DB/Redis passwords together without exposing their values.

Run inside the current API container with the project mounted writable at /release.
The configured PostgreSQL role must be the role used by the application.
"""
import argparse
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, '/app')
from psycopg import sql
from app.db import engine

from offline_tool import read_env


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--env', required=True)
    args = parser.parse_args()
    path = Path(args.env).resolve()
    values = read_env(path)
    username = values.get('POSTGRES_USER', 'rag')
    database = values.get('POSTGRES_DB', 'rag')
    if engine.url.username != username:
        raise SystemExit('Configured database role differs from the application role')
    db_password, redis_password = secrets.token_hex(24), secrets.token_hex(24)
    changes = {
        'POSTGRES_PASSWORD': db_password,
        'REDIS_PASSWORD': redis_password,
        'DATABASE_URL': f'postgresql+psycopg://{quote(username, safe="")}:{db_password}@postgres:5432/{quote(database, safe="")}',
        'REDIS_URL': f'redis://:{redis_password}@redis:6379/0',
        'COOKIE_SECURE': 'true',
    }
    lines, seen = [], set()
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        key = line.split('=', 1)[0].strip() if '=' in line and not line.lstrip().startswith('#') else ''
        if key in changes:
            if key not in seen:
                lines.append(key + '=' + changes[key]); seen.add(key)
        else:
            lines.append(line)
    lines.extend(key + '=' + value for key, value in changes.items() if key not in seen)
    temporary = path.with_name('.env.rotation-' + secrets.token_hex(6))
    temporary.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    temporary.chmod(0o600)
    # Keep this authenticated connection open until the configuration swap finishes.
    connection = None
    try:
        connection = engine.raw_connection()
        driver = connection.driver_connection
        with driver.cursor() as cursor:
            cursor.execute(sql.SQL('ALTER ROLE {} PASSWORD {}').format(sql.Identifier(username), sql.Literal(db_password)))
        driver.commit()
        try:
            os.replace(temporary, path)
        except Exception:
            with driver.cursor() as cursor:
                cursor.execute(sql.SQL('ALTER ROLE {} PASSWORD {}').format(
                    sql.Identifier(username), sql.Literal(values['POSTGRES_PASSWORD'])))
            driver.commit()
            raise
    finally:
        if connection is not None:
            connection.close()
        temporary.unlink(missing_ok=True)
    print('Local database and Redis credentials rotated; restart the services to apply the new configuration.')


if __name__ == '__main__':
    main()
