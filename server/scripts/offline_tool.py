"""Standard-library release tooling. Never evaluates configuration as shell code."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import tarfile
from pathlib import Path


def read_env(path: Path) -> dict[str, str]:
    values = {}
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def image_content(image: dict) -> dict:
    """Engine-independent content identity: OCI indexes and config IDs may differ."""
    config = image.get('Config') or {}
    normalized = {}
    for key in ('Env', 'Cmd', 'Entrypoint'):
        normalized[key] = config.get(key) or []
    for key in ('User', 'WorkingDir', 'StopSignal'):
        normalized[key] = config.get(key) or ''
    normalized['Labels'] = config.get('Labels') or {}
    for key in ('ExposedPorts', 'Volumes'):
        normalized[key] = sorted((config.get(key) or {}).keys())
    return {
        'os': image.get('Os', ''),
        'architecture': image.get('Architecture', ''),
        'layers': (image.get('RootFS') or {}).get('Layers') or [],
        'config': normalized,
    }


def image_fingerprint(image: dict) -> str:
    canonical = json.dumps(image_content(image), sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return 'sha256:' + hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def image_manifest(inspected: list[dict], names: list[str]) -> dict:
    result = {}
    for name in names:
        image = next((item for item in inspected if name in (item.get('RepoTags') or [])), None)
        if image is None:
            raise ValueError(f'Missing release image tag: {name}')
        if image.get('Os') != 'linux' or image.get('Architecture') != 'amd64':
            raise ValueError(f'Unexpected release image platform: {name}')
        result[name] = {'image_id': image.get('Id', ''), 'fingerprint': image_fingerprint(image), 'content': image_content(image)}
    return {'release': '2.2.0', 'platform': 'linux/amd64', 'images': result}


def verify_images(inspected: list[dict], manifest: dict):
    actual = image_manifest(inspected, list(manifest['images']))
    for name, expected in manifest['images'].items():
        if actual['images'][name]['fingerprint'] != expected['fingerprint']:
            raise ValueError(f'Image content fingerprint mismatch: {name}')


def manifest_path(models: Path, name: str) -> Path:
    # Only explicit local Ollama library names are supported by this release tool.
    if not re.fullmatch(r'[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)?', name):
        raise ValueError(f'Unsupported local model name: {name}')
    model, _, tag = name.partition(':')
    return models / 'manifests' / 'registry.ollama.ai' / 'library' / model / (tag or 'latest')


def verify_models(models: Path, names: list[str]) -> dict:
    output = {}
    checked = {}
    for name in dict.fromkeys(names):
        manifest = manifest_path(models, name)
        if not manifest.is_file():
            raise ValueError(f'Missing model manifest: {name}')
        content = json.loads(manifest.read_text())
        refs = [content['config'], *content['layers']]
        blobs = {}
        for ref in refs:
            digest = ref['digest']
            if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
                raise ValueError(f'Invalid blob digest in model {name}')
            blob = models / 'blobs' / digest.replace(':', '-')
            if not blob.is_file() or blob.stat().st_size != ref['size']:
                raise ValueError(f'Missing or truncated model blob: {digest}')
            actual = checked.setdefault(digest, None)
            if actual is None:
                actual = checked[digest] = sha256(blob)
            if actual != digest.split(':', 1)[1]:
                raise ValueError(f'Model blob checksum mismatch: {digest}')
            blobs[str(blob.relative_to(models)).replace('\\', '/')] = actual
        output[name] = {
            'manifest': str(manifest.relative_to(models)).replace('\\', '/'),
            'manifest_sha256': sha256(manifest),
            'blobs': blobs,
        }
    return output


def configured_models(env: dict) -> list[str]:
    names = [env.get('OLLAMA_CHAT_MODEL', 'qwen3:4b-instruct'), env.get('OLLAMA_EMBED_MODEL', 'embeddinggemma')]
    if env.get('VISION_ENABLED', 'false').lower() in ('true', '1', 'yes'):
        names.append(env.get('OLLAMA_VISION_MODEL', 'qwen2.5vl:3b'))
    return names


def safe_extract(archive: Path, target: Path):
    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, 'r:gz') as handle:
        for member in handle.getmembers():
            path = (target / member.name).resolve()
            if not path.is_relative_to(target) or member.issym() or member.islnk() or member.isdev():
                raise ValueError('Archive contains an unsafe path or link')
        handle.extractall(target, filter='data')


def main():
    # Git Bash callers require LF output even when the preparer uses Windows Python.
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(newline='\n')
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)
    command = commands.add_parser('models-check')
    command.add_argument('--models', type=Path, required=True)
    command.add_argument('--env', type=Path, default=Path('.env'))
    command.add_argument('--report', type=Path)
    command.add_argument('--release-report', type=Path)
    command = commands.add_parser('models-pack')
    command.add_argument('--models', type=Path, required=True)
    command.add_argument('--env', type=Path, default=Path('.env'))
    command.add_argument('--output', type=Path, required=True)
    command = commands.add_parser('init-env')
    command.add_argument('--root', type=Path, default=Path('.'))
    command = commands.add_parser('config-check')
    command.add_argument('--env', type=Path, default=Path('.env'))
    command = commands.add_parser('apply-runtime-config')
    command.add_argument('--runtime', type=Path, required=True)
    command.add_argument('--env', type=Path, required=True)
    command.add_argument('--new-env', action='store_true')
    command = commands.add_parser('image-refs')
    command.add_argument('--lock', type=Path, default=Path('deploy/images.lock.json'))
    command = commands.add_parser('image-manifest')
    command.add_argument('--inspect', type=Path, required=True)
    command.add_argument('--tags', type=Path, required=True)
    command.add_argument('--output', type=Path, required=True)
    command = commands.add_parser('image-verify')
    command.add_argument('--inspect', type=Path, required=True)
    command.add_argument('--manifest', type=Path, required=True)
    command = commands.add_parser('compose-files')
    command.add_argument('--env', type=Path, required=True)
    command.add_argument('--root', type=Path, required=True)
    command = commands.add_parser('safe-extract')
    command.add_argument('archive', type=Path)
    command.add_argument('target', type=Path)
    command = commands.add_parser('data-pack')
    command.add_argument('--data', type=Path, required=True)
    command.add_argument('--output', type=Path, required=True)
    command = commands.add_parser('tree-pack')
    command.add_argument('source', type=Path)
    command.add_argument('archive', type=Path)
    args = parser.parse_args()
    if args.command == 'image-refs':
        lock = json.loads(args.lock.read_text())
        for target, source in lock['images'].items():
            print(target, source, sep='\t')
    elif args.command == 'image-manifest':
        inspected = json.loads(args.inspect.read_text(encoding='utf-8-sig'))
        names = [line.split()[0] for line in args.tags.read_text().splitlines() if line.strip()]
        report = image_manifest(inspected, names)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
        print(f'Recorded portable content fingerprints for {len(names)} images.')
    elif args.command == 'image-verify':
        inspected = json.loads(args.inspect.read_text(encoding='utf-8-sig'))
        report = json.loads(args.manifest.read_text(encoding='utf-8-sig'))
        verify_images(inspected, report)
        print(f'Verified portable content fingerprints for {len(report["images"])} images.')
    elif args.command == 'compose-files':
        env = read_env(args.env)
        root = args.root.resolve()
        separator = env.get('COMPOSE_PATH_SEPARATOR') or os.pathsep
        selected = env.get('COMPOSE_FILE') or 'docker-compose.yml'
        for name in selected.split(separator):
            path = (root / name).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError('Configured Compose files must exist inside the recovery installation')
            print(path.relative_to(root).as_posix())
    elif args.command == 'init-env':
        path = args.root / '.env'
        if path.exists():
            print('Existing .env retained; no credentials changed.')
            return
        text = (args.root / '.env.example').read_text()
        for key in ('POSTGRES_PASSWORD', 'REDIS_PASSWORD', 'CHROMA_AUTH_TOKEN'):
            text = re.sub(rf'(?m)^{key}=.*$', f'{key}={secrets.token_hex(24)}', text)
        with path.open('x') as output:
            output.write(text)
        os.chmod(path, 0o600)
        print('Created .env with random credentials. Set the server hostname and paths before starting.')
    elif args.command == 'config-check':
        if not args.env.is_file():
            raise ValueError('Missing .env; run init-config.sh first')
        env = read_env(args.env)
        for key in ('POSTGRES_PASSWORD', 'REDIS_PASSWORD'):
            value = env.get(key, '')
            if len(value) < 24 or not value.isascii() or not value.isalnum() or 'change' in value.lower():
                raise ValueError(f'{key} must be a random alphanumeric value of at least 24 characters')
        print('Configuration credentials validated.')
    elif args.command == 'apply-runtime-config':
        runtime = json.loads(args.runtime.read_text())
        allowed = {'OLLAMA_CHAT_MODEL', 'OLLAMA_EMBED_MODEL', 'OLLAMA_VISION_MODEL', 'VISION_ENABLED'}
        if set(runtime) != allowed:
            raise ValueError('Unexpected release model configuration')
        if runtime['VISION_ENABLED'] not in ('true', 'false'):
            raise ValueError('Invalid release vision configuration')
        for key in allowed - {'VISION_ENABLED'}:
            manifest_path(Path('.'), runtime[key])
        env = read_env(args.env)
        if args.new_env:
            text = args.env.read_text()
            for key, value in runtime.items():
                text = re.sub(rf'(?m)^{key}=.*$', f'{key}={value}', text)
            args.env.write_text(text)
            os.chmod(args.env, 0o600)
            print('Configured the new installation with the packaged model names.')
        else:
            defaults = {'OLLAMA_CHAT_MODEL': 'qwen3:4b-instruct', 'OLLAMA_EMBED_MODEL': 'embeddinggemma', 'VISION_ENABLED': 'false', 'OLLAMA_VISION_MODEL': 'qwen2.5vl:3b'}
            for key, value in runtime.items():
                if key == 'OLLAMA_VISION_MODEL' and runtime['VISION_ENABLED'] == 'false':
                    continue
                configured = env.get(key, defaults[key])
                if key == 'VISION_ENABLED':
                    configured = 'true' if configured.lower() in ('true', '1', 'yes') else 'false'
                if configured != value:
                    raise ValueError(f'Existing {key} differs from the release; retain your configuration and prepare a matching model release')
            print('Existing model configuration matches the release; no settings changed.')
    elif args.command in ('models-check', 'models-pack'):
        env = read_env(args.env)
        if args.command == 'models-check' and args.release_report:
            release = json.loads(args.release_report.read_text())
            names = list(release)
        else:
            release = None
            names = configured_models(env)
        report = verify_models(args.models, names)
        if release is not None and report != release:
            raise ValueError('Model manifests differ from the release manifest')
        if args.command == 'models-check':
            if args.report:
                args.report.write_text(json.dumps(report, indent=2) + '\n')
            print(f'Verified {len(report)} models and all referenced blobs.')
        else:
            files = set()
            for data in report.values():
                files.add(data['manifest'])
                files.update(data['blobs'])
            with tarfile.open(args.output, 'w:gz', compresslevel=1) as archive:
                for name in sorted(files):
                    archive.add(args.models / name, arcname='models/' + name, recursive=False)
            args.output.with_name('models.json').write_text(json.dumps(report, indent=2) + '\n')
            runtime = {
                'OLLAMA_CHAT_MODEL': env.get('OLLAMA_CHAT_MODEL', 'qwen3:4b-instruct'),
                'OLLAMA_EMBED_MODEL': env.get('OLLAMA_EMBED_MODEL', 'embeddinggemma'),
                'OLLAMA_VISION_MODEL': env.get('OLLAMA_VISION_MODEL', 'qwen2.5vl:3b'),
                'VISION_ENABLED': 'true' if env.get('VISION_ENABLED', 'false').lower() in ('true', '1', 'yes') else 'false',
            }
            args.output.with_name('runtime.json').write_text(json.dumps(runtime, indent=2) + '\n')
            print(f'Packed {len(report)} models without Ollama keys or user files.')
    elif args.command == 'safe-extract':
        safe_extract(args.archive, args.target)
    elif args.command == 'data-pack':
        # Model blobs are immutable and supplied with the release. User docs and staging are backed up.
        with tarfile.open(args.output, 'w:gz') as archive:
            for path in sorted(args.data.iterdir()):
                if path.name != 'ollama' and not path.is_symlink():
                    archive.add(path, arcname=path.name)
    elif args.command == 'tree-pack':
        with tarfile.open(args.archive, 'w:gz') as archive:
            for path in sorted(args.source.iterdir()):
                if path.is_symlink():
                    raise ValueError('Snapshot source must not contain symlinks')
                archive.add(path, arcname=path.name)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, json.JSONDecodeError, tarfile.TarError) as error:
        raise SystemExit(str(error)) from error
