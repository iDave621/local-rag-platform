"""Offline preparation/provisioning inside the bundled backend image.

RUN.sh/RUN.ps1 orchestrate Docker on the host. This helper never starts Docker,
downloads software, replaces existing credentials, or copies source passwords.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

RELEASE = '2.2.0'
DEFAULTS = {
    'https_port': 443, 'http_redirect_port': 80,
    'web_bind_address': '0.0.0.0', 'admin_username': 'admin',
    'server_names': [], 'preload_documents': False,
}


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def atomic_write(path, contents, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8', newline='\n') as output:
            output.write(contents)
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path, value):
    atomic_write(path, json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def valid_name(value):
    if not isinstance(value, str) or not value or len(value) > 253:
        return False
    try:
        return isinstance(ipaddress.ip_address(value), ipaddress.IPv4Address)
    except ValueError:
        if re.fullmatch(r'[0-9]+(?:\.[0-9]+){3}', value):
            return False
        return all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', part)
                   for part in value.split('.'))


def load_config(root):
    supplied = read_json(root / 'run-config.json')
    if not isinstance(supplied, dict):
        raise ValueError('run-config.json must contain a JSON object')
    config = {**DEFAULTS, **supplied}
    if set(config) != set(DEFAULTS):
        raise ValueError('run-config.json contains unsupported settings')
    for field in ('https_port', 'http_redirect_port'):
        if type(config[field]) is not int or not 1 <= config[field] <= 65535:
            raise ValueError(field + ' must be a port between 1 and 65535')
    if config['https_port'] == config['http_redirect_port']:
        raise ValueError('HTTPS and HTTP redirect need different ports')
    if not isinstance(config['web_bind_address'], str):
        raise ValueError('web_bind_address must be an IPv4 address')
    try:
        if not isinstance(ipaddress.ip_address(config['web_bind_address']), ipaddress.IPv4Address):
            raise ValueError('Use an IPv4 web_bind_address')
    except ValueError as error:
        raise ValueError('web_bind_address must be an IPv4 address') from error
    if not isinstance(config['admin_username'], str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{3,80}', config['admin_username']):
        raise ValueError('admin_username must contain 3 to 80 letters, digits, dots, @, underscores or hyphens')
    if not isinstance(config['server_names'], list) or any(not valid_name(name) for name in config['server_names']):
        raise ValueError('server_names must be a list of DNS names or IPv4 addresses')
    if type(config['preload_documents']) is not bool:
        raise ValueError('preload_documents must be true or false')
    return config


def load_tool(root):
    path = root / 'app/server/scripts/offline_tool.py'
    specification = importlib.util.spec_from_file_location('standalone_offline_tool', path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def host_path(value):
    value = value.replace('\\', '/')
    if not (value.startswith('/') or re.match(r'^[A-Za-z]:/', value)):
        raise ValueError('The runner must provide an absolute host directory')
    if any(character in value for character in "\r\n\0'\""):
        raise ValueError('Use an installation directory without quotes or line breaks')
    if value == '/' or re.fullmatch(r'[A-Za-z]:/', value):
        raise ValueError('Use a dedicated installation folder rather than a filesystem root')
    return value.rstrip('/')


def update_env(path, settings):
    lines = path.read_text(encoding='utf-8-sig').splitlines()
    present = set()
    for index, line in enumerate(lines):
        key = line.split('=', 1)[0].strip() if '=' in line else ''
        if key in settings:
            lines[index] = key + '=' + settings[key]
            present.add(key)
    lines.extend(key + '=' + value for key, value in settings.items() if key not in present)
    atomic_write(path, '\n'.join(lines) + '\n')


def model_files_present(tool, models, report):
    for name, entry in report.items():
        path = models / entry['manifest']
        if not path.is_file() or tool.sha256(path) != entry['manifest_sha256']:
            return False
        manifest = read_json(path)
        for blob in [manifest.get('config', {}), *manifest.get('layers', [])]:
            digest = blob.get('digest', '').removeprefix('sha256:')
            path = models / 'blobs' / ('sha256-' + digest)
            if not path.is_file() or path.stat().st_size != blob.get('size'):
                return False
    return True


def prepare(root, args):
    config = load_config(root)
    tool = load_tool(root)
    payload, app, runtime = root / 'payload', root / 'app', root / 'runtime'
    runtime.mkdir(exist_ok=True)
    native = host_path(args.host_root)
    tool.verify_images(read_json(runtime / 'image-inspection.json'), read_json(payload / 'images.json'))
    print('Verified the seven packaged images.', flush=True)
    env_path = app / '.env'
    new_env = not env_path.exists()
    subprocess.run([sys.executable, str(app / 'server/scripts/offline_tool.py'),
                    'init-env', '--root', str(app)], check=True)
    command = [sys.executable, str(app / 'server/scripts/offline_tool.py'),
               'apply-runtime-config', '--runtime', str(payload / 'runtime.json'),
               '--env', str(env_path)]
    if new_env:
        command.append('--new-env')
    subprocess.run(command, check=True)
    env = tool.read_env(env_path)
    project = env.get('COMPOSE_PROJECT_NAME') or 'local-rag-standalone-' + uuid.uuid4().hex[:12]
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', project):
        raise ValueError('Invalid existing Compose project name')
    update_env(env_path, {
        'COMPOSE_PROJECT_NAME': project,
        'COMPOSE_FILE': 'docker-compose.yml:deploy/compose/offline-validation.yml',
        'COMPOSE_PATH_SEPARATOR': ':',
        'HOST_DATA_PATH': "'" + native + "/app/data'",
        'OLLAMA_HOST_PATH': "'" + native + "/app/data/ollama'",
        'TLS_CERT_PATH': "'" + native + "/app/deploy/tls'",
        'HTTPS_PORT': str(config['https_port']),
        'HTTP_REDIRECT_PORT': str(config['http_redirect_port']),
        'WEB_BIND_ADDRESS': config['web_bind_address'],
        'COOKIE_SECURE': 'true',
        'LITERATURE_SITE_URL': '',
    })
    subprocess.run([sys.executable, str(app / 'server/scripts/offline_tool.py'),
                    'config-check', '--env', str(env_path)], check=True)
    report = read_json(payload / 'models.json')
    destination = app / 'data/ollama'
    destination.mkdir(parents=True, exist_ok=True)
    models = destination / 'models'
    marker_path = runtime / 'install.json'
    marker = read_json(marker_path) if marker_path.exists() else {}
    report_hash = tool.sha256(payload / 'models.json')
    cached = marker.get('models_manifest_sha256') == report_hash and model_files_present(tool, models, report)
    if not cached:
        if models.exists() and any(models.iterdir()):
            actual = tool.verify_models(models, list(report))
            if actual != report:
                raise ValueError('The existing model store differs from the package; it was retained')
        else:
            staging = runtime / ('model-import-' + uuid.uuid4().hex)
            staging.mkdir()
            print('Extracting and verifying both local Ollama models. No download is used.', flush=True)
            tool.safe_extract(payload / 'models.tar.gz', staging)
            if tool.verify_models(staging / 'models', list(report)) != report:
                raise ValueError('Extracted models differ from their release manifest')
            if models.exists():
                models.rmdir()  # Only the empty directory can be replaced.
            (staging / 'models').rename(models)
            staging.rmdir()
    print('Both Ollama models are present and match this release.', flush=True)
    names = list(dict.fromkeys(['localhost', '127.0.0.1', *config['server_names'],
                               *[name for name in args.server_name if valid_name(name)]]))
    tls = app / 'deploy/tls'
    if not (tls / 'server.crt').exists() and not (tls / 'server.key').exists():
        subprocess.run(['bash', str(app / 'server/scripts/generate-lan-tls.sh'), *names], check=True)
    for filename in ('server.crt', 'server.key', 'ca.crt'):
        if not (tls / filename).is_file():
            raise ValueError('TLS setup is incomplete; existing files were retained')
    subprocess.run(['openssl', 'verify', '-CAfile', str(tls / 'ca.crt'),
                    str(tls / 'server.crt')], check=True)
    subprocess.run(['openssl', 'x509', '-in', str(tls / 'server.crt'),
                    '-checkend', '0', '-noout'], check=True)
    for name in dict.fromkeys(['localhost', '127.0.0.1', *config['server_names']]):
        option = '-checkip' if re.fullmatch(r'[0-9]+(?:\.[0-9]+){3}', name) else '-checkhost'
        result = subprocess.run(['openssl', 'x509', '-in', str(tls / 'server.crt'),
                                 option, name, '-noout'], capture_output=True, text=True)
        if result.returncode:
            raise ValueError('The retained HTTPS certificate does not cover ' + name +
                             '. Follow the certificate renewal instructions before restarting.')
    write_json(marker_path, {'release': RELEASE, 'project': project, 'host_root': native,
                            'server_names': names, 'models_manifest_sha256': report_hash,
                            'prepared_at': datetime.now(timezone.utc).isoformat()})
    print('Local configuration, model store and HTTPS are ready.', flush=True)


def credentials(root):
    path = root / 'runtime/admin-login.txt'
    if not path.exists():
        return {}
    return dict(line.split(': ', 1) for line in path.read_text().splitlines() if ': ' in line)


def seed_entries(root, tool):
    seed_root = root / 'seed-documents'
    if not (seed_root / 'documents.json').exists():
        return []
    manifest = read_json(seed_root / 'documents.json')
    if not isinstance(manifest, dict):
        raise ValueError('Invalid seed document metadata')
    entries = manifest.get('documents')
    if not isinstance(entries, list):
        raise ValueError('Invalid seed document metadata')
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('Invalid seed document metadata')
        name = entry.get('filename', '')
        if not isinstance(name, str) or not name or Path(name).name != name or '/' in name or '\\' in name or Path(name).suffix.lower() not in {'.pdf', '.docx', '.doc'}:
            raise ValueError('Invalid packaged document filename')
        path = seed_root / name
        digest = entry.get('sha256', '')
        if path.is_symlink() or not path.is_file() or not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
            raise ValueError('Invalid packaged document path or digest')
        if tool.sha256(path) != entry['sha256']:
            raise ValueError('Packaged document checksum differs: ' + name)
        if entry.get('origin') != 'admin' or type(entry.get('version_rank')) is not int:
            raise ValueError('Invalid packaged document identity')
    return entries


def provision(root):
    sys.path.insert(0, '/app')
    from sqlalchemy import select
    from app.config import get_settings
    from app.db import User, db_session
    from app.ingestion import ingest_path
    from app.security import hash_password, verify_password

    config, tool = load_config(root), load_tool(root)
    username = config['admin_username']
    saved = credentials(root)
    created = False
    with db_session() as db:
        user = db.scalar(select(User).where(User.username == username))
        if user is None:
            password = secrets.token_urlsafe(24)
            db.add(User(username=username, password_hash=hash_password(password), role='admin', active=True))
            atomic_write(root / 'runtime/admin-login.txt', 'Username: ' + username + '\nPassword: ' + password + '\n')
            created = True
        elif user.role != 'admin' or not user.active:
            raise ValueError('The configured username already belongs to a disabled or non-admin account')
        elif saved.get('Username') == username and not verify_password(saved.get('Password', ''), user.password_hash):
            print('The existing admin password was retained; its saved login file is outdated.', flush=True)
            atomic_write(root / 'runtime/admin-login.txt', 'Username: ' + username + '\nPassword changed: use your current password\n')
    print('Created a new admin account.' if created else 'Existing admin account retained.', flush=True)
    provision_path = root / 'runtime/provision.json'
    previous = read_json(provision_path) if provision_path.exists() else {}
    completed_seeds = dict(previous.get('completed_seeds', {}))
    state = {'release': RELEASE, 'admin_username': username,
             'seed_documents_enabled': config['preload_documents'],
             'completed_seeds': completed_seeds}
    if config['preload_documents']:
        for entry in seed_entries(root, tool):
            seed_id = hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()
            if seed_id in completed_seeds:
                continue  # An admin may have intentionally removed this document.
            directory = get_settings().data_path / 'seed' / entry['sha256']
            directory.mkdir(parents=True, exist_ok=True)
            destination = directory / entry['filename']
            if not destination.exists():
                shutil.copyfile(root / 'seed-documents' / entry['filename'], destination)
            if tool.sha256(destination) != entry['sha256']:
                raise ValueError('The retained seed source differs; existing data was not overwritten')
            result = ingest_path(str(destination), 'admin', source_name=entry.get('source_name', entry['filename']),
                                 topic_key=entry.get('topic_key'), version_rank=entry['version_rank'],
                                 version_label=entry.get('version_label'))
            print('Knowledge document ' + entry['filename'] + ': ' + result['status'], flush=True)
            completed_seeds[seed_id] = result['status']
            write_json(provision_path, state)
    state['completed_at'] = datetime.now(timezone.utc).isoformat()
    write_json(provision_path, state)


def show(root):
    config = load_config(root)
    suffix = '' if config['https_port'] == 443 else ':' + str(config['https_port'])
    print('\nOpen https://localhost' + suffix, flush=True)
    for name in config['server_names']:
        print('LAN URL: https://' + name + suffix, flush=True)
    saved = credentials(root)
    if saved:
        print('Username: ' + saved.get('Username', config['admin_username']), flush=True)
        if 'Password' in saved:
            print('Password: ' + saved['Password'], flush=True)
        else:
            print('Use the current password for your existing admin account.', flush=True)
    print('Login details are saved privately in runtime/admin-login.txt.', flush=True)
    print('Trust app/deploy/tls/ca.crt on each browser client for HTTPS.', flush=True)
    print('Keep this installed folder and its Docker volumes; stop preserves all data.', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'provision', 'show'])
    parser.add_argument('--host-root')
    parser.add_argument('--server-name', action='append', default=[])
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    if args.action == 'prepare':
        if not args.host_root:
            parser.error('prepare requires --host-root')
        prepare(root, args)
    elif args.action == 'provision':
        provision(root)
    else:
        show(root)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        raise SystemExit('Offline setup failed: ' + str(error)) from error
