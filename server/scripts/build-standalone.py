"""Build the transferable standalone release using only the host standard library."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
from pathlib import Path, PurePosixPath


VERSION = '2.2.0'
PACKAGE_NAME = f'local-rag-standalone-{VERSION}'
CHUNK_SIZE = 8 * 1024 * 1024
PAYLOAD_FILES = (
    'images.tar.gz', 'models.tar.gz', 'models.json', 'runtime.json',
    'images.tsv', 'images.json', 'sources.json', 'release.txt',
)
LAUNCHER_FILES = ('RUN.cmd', 'RUN.ps1', 'RUN.sh', 'setup.py', 'run-config.json', 'README.txt')
APP_FILES = (
    'docker-compose.yml', '.env.example', '.dockerignore', '.gitignore', '.gitattributes',
    'README.md', 'LOCAL_TEST.md', 'Makefile', 'backend/Dockerfile',
    'backend/requirements.txt', 'backend/requirements.lock.txt',
    'frontend/Dockerfile', 'frontend/package.json', 'frontend/package-lock.json',
    'frontend/index.html', 'frontend/vite.config.js', 'frontend/nginx.conf',
    'redis/redis.conf', 'deploy/images.lock.json',
    'deploy/OFFLINE_DEPLOYMENT.md', 'deploy/BACKUP_RESTORE.md',
    'deploy/SPLIT_DEPLOYMENT.md',
)
DIRECTORIES = {
    'docs': {'.md', '.svg', '.png'},
    '.github': {'.yml', '.yaml', '.md'},
    'backend/app': {'.py'},
    'frontend/src': {'.js', '.jsx', '.css', '.json', '.svg', '.png', '.jpg', '.jpeg', '.webp', '.woff', '.woff2'},
    'tests': {'.py', '.mjs', '.js', '.sh', '.json', '.md', '.txt', '.pdf', '.docx', '.doc', '.png', '.svg', '.yaml', '.yml'},
    'server/scripts': {'.py', '.sh', '.ps1', '.cmd', '.md', '.txt', '.json', '.conf'},
    'server/standalone': {'.py', '.sh', '.ps1', '.cmd', '.md', '.txt', '.json'},
    'deploy/compose': {'.yaml', '.yml'},
    'deploy/nginx': {'.conf', '.template'},
    'deploy/postgres': {'.conf'},
}
EXCLUDED_DIRS = {
    '.git', '.agents', '.codex', '.aws', '__pycache__', 'node_modules', 'dist',
    'data', 'models', 'model-store', 'tls', 'pki', 'credentials', 'runtime',
    'smoke', 'temp', 'tmp', '.tmp', '.cache', '.venv', 'venv',
}
TEXT_EXTENSIONS = {
    '.py', '.js', '.jsx', '.mjs', '.css', '.html', '.json', '.txt', '.md',
    '.sh', '.ps1', '.cmd', '.yaml', '.yml', '.conf', '.template', '.sql', '.modelfile',
}
HASH_LINE = re.compile(r'^([0-9a-fA-F]{64}) [ *](.+)$')
DOCUMENT_EXTENSIONS = {'.pdf', '.docx', '.doc'}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(CHUNK_SIZE), b''):
            digest.update(block)
    return digest.hexdigest()


def safe_regular(path: Path, root: Path) -> Path:
    """Reject links, including links in ancestors, before reading allowlisted files."""
    root = Path(os.path.abspath(root))
    path = Path(os.path.abspath(path))
    if not path.is_relative_to(root) or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f'Path escapes its source directory: {path}')
    cursor = path
    while cursor != root:
        if cursor.is_symlink():
            raise ValueError(f'Symbolic links are not packaged: {path}')
        cursor = cursor.parent
    if not path.is_file():
        raise ValueError(f'Required regular file is missing: {path}')
    return path


def manifest_entries(path: Path) -> dict[str, str]:
    safe_regular(path, path.parent)
    entries = {}
    for number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
        if not line:
            continue
        match = HASH_LINE.fullmatch(line)
        if not match:
            raise ValueError(f'Invalid checksum line {number}: {path}')
        digest, name = match.groups()
        relative = PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts or '\\' in name or name != relative.as_posix():
            raise ValueError(f'Unsafe checksum path: {name}')
        if name in entries:
            raise ValueError(f'Duplicate checksum path: {name}')
        entries[name] = digest.lower()
    return entries


def verified_payload(release: Path) -> dict[str, str]:
    expected = manifest_entries(release / 'SHA256SUMS')
    verified = {}
    for name in PAYLOAD_FILES:
        if name not in expected:
            raise ValueError(f'Release checksum manifest does not contain {name}')
        source = safe_regular(release / name, release)
        print(f'Verifying release payload: {name}', flush=True)
        actual = sha256(source)
        if actual != expected[name]:
            raise ValueError(f'Release payload checksum mismatch: {name}')
        verified[name] = actual
    release_values = dict(line.split('=', 1) for line in (release / 'release.txt').read_text().splitlines() if '=' in line)
    if release_values.get('release') != VERSION or release_values.get('platform') != 'linux/amd64':
        raise ValueError(f'The standalone package requires release {VERSION} for linux/amd64')
    return verified


def copy_file(source: Path, destination: Path, *, normalize=False, hardlink=False):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if hardlink:
        try:
            os.link(source, destination)
            return
        except OSError:
            pass
    if normalize:
        data = source.read_bytes().replace(b'\r\n', b'\n')
        if source.suffix.lower() == '.cmd':
            data = data.replace(b'\n', b'\r\n')
        destination.write_bytes(data)
    else:
        shutil.copyfile(source, destination)


def source_files(project: Path) -> list[Path]:
    selected = set()
    for relative in APP_FILES:
        path = project / relative
        if path.exists():
            selected.add(safe_regular(path, project))
        else:
            raise ValueError(f'Application source file is missing: {relative}')
    for relative, extensions in DIRECTORIES.items():
        directory = project / relative
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError(f'Application source directory is missing or linked: {relative}')
        for current, names, files in os.walk(directory, followlinks=False):
            current = Path(current)
            names[:] = sorted(name for name in names if name.lower() not in EXCLUDED_DIRS
                              and not name.lower().startswith(('.smoke', 'smoke-', 'tmp-', 'temp-')))
            for name in sorted(files):
                path = current / name
                lowered = name.lower()
                if (lowered.startswith(('.env', 'credentials', 'id_ed25519', 'id_rsa'))
                        or lowered.endswith(('.partial', '.next', '.log', '.tmp'))):
                    continue
                if path.suffix.lower() in extensions:
                    selected.add(safe_regular(path, project))
    return sorted(selected, key=lambda path: path.relative_to(project).as_posix())


def safe_basename(value, label) -> str:
    if (not isinstance(value, str) or not value or value in {'.', '..'}
            or '/' in value or '\\' in value or any(ord(char) < 32 for char in value)
            or len(value) > 200 or Path(value).name != value
            or any(char in value for char in '<>:"|?*') or value.endswith((' ', '.'))
            or re.fullmatch(r'(?i)(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', value)):
        raise ValueError(f'Invalid seed {label}: {value!r}')
    return value


def verified_seeds(directory: Path | None) -> list[Path]:
    if directory is None or not directory.exists():
        return []
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError('Seed documents must be supplied in a regular directory')
    metadata = safe_regular(directory / 'documents.json', directory)
    report = json.loads(metadata.read_text(encoding='utf-8-sig'))
    documents = report.get('documents') if isinstance(report, dict) else None
    if not isinstance(documents, list):
        raise ValueError('Seed documents.json must contain a documents list')
    names = {'documents.json'}
    for document in documents:
        if not isinstance(document, dict):
            raise ValueError('Seed document entries must be objects')
        name = safe_basename(document.get('filename'), 'filename')
        source_name = safe_basename(document.get('source_name'), 'source_name')
        if Path(name).suffix.lower() not in DOCUMENT_EXTENSIONS or Path(source_name).suffix.lower() not in DOCUMENT_EXTENSIONS:
            raise ValueError(f'Unsupported seed document type: {name}')
        if name in names:
            raise ValueError(f'Duplicate seed filename: {name}')
        origin, topic = document.get('origin'), document.get('topic_key')
        rank, version = document.get('version_rank'), document.get('version_label')
        digest = document.get('sha256')
        if origin != 'admin':
            raise ValueError(f'Invalid seed origin: {name}')
        if not isinstance(topic, str) or not 1 <= len(topic) <= 255 or any(ord(char) < 32 for char in topic):
            raise ValueError(f'Invalid seed document topic: {name}')
        if isinstance(rank, bool) or not isinstance(rank, int) or not 0 <= rank <= 2147483647:
            raise ValueError(f'Invalid seed document version rank: {name}')
        if not isinstance(version, str) or not 1 <= len(version) <= 80:
            raise ValueError(f'Invalid seed document version label: {name}')
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError(f'Invalid seed document checksum: {name}')
        source = safe_regular(directory / name, directory)
        if sha256(source) != digest.lower():
            raise ValueError(f'Seed document checksum mismatch: {name}')
        names.add(name)
    actual = {path.name for path in directory.iterdir()}
    if actual != names:
        raise ValueError(f'Seed directory contains undeclared files: {sorted(actual - names)}')
    return [directory / name for name in sorted(names)]


def stage_package(project: Path, release: Path, stage_root: Path, seed_directory: Path | None) -> Path:
    offline = (project / 'offline').resolve()
    if not stage_root.resolve().is_relative_to(offline) or stage_root.resolve() == offline:
        raise ValueError('--stage-root must be a dedicated directory inside this project\'s offline directory')
    if stage_root.is_symlink():
        raise ValueError('The build staging directory must not be a link')
    package = stage_root / PACKAGE_NAME
    if package.exists() and (not package.is_dir() or package.is_symlink() or any(package.iterdir())):
        raise ValueError(f'Package staging is not empty. Choose a fresh --stage-root: {package}')
    # Check every source before creating a stage. Failed builds are never deleted recursively.
    launchers = [safe_regular(project / 'server' / 'standalone' / name, project) for name in LAUNCHER_FILES]
    application = source_files(project)
    seeds = verified_seeds(seed_directory)
    verified = verified_payload(release)
    package.mkdir(parents=True, exist_ok=True)
    for source in launchers:
        copy_file(source, package / source.name, normalize=True)
    for name in PAYLOAD_FILES:
        copy_file(release / name, package / 'payload' / name, hardlink=name.endswith('.tar.gz'))
    for source in application:
        destination = package / 'app' / source.relative_to(project)
        text = source.suffix.lower() in TEXT_EXTENSIONS or source.name in {'.env.example', '.dockerignore', '.gitignore', 'Makefile'}
        copy_file(source, destination, normalize=text)
    for source in seeds:
        copy_file(source, package / 'seed-documents' / source.name)
    checksums = {}
    for source in sorted(package.rglob('*')):
        if source.is_file():
            relative = source.relative_to(package).as_posix()
            # Operators can edit first-launch configuration; runtime state is not staged.
            if relative == 'run-config.json':
                continue
            checksums[relative] = verified[source.name] if relative.startswith('payload/') else sha256(source)
    (package / 'SHA256SUMS').write_text(''.join(f'{checksums[name]}  {name}\n' for name in sorted(checksums)), encoding='utf-8', newline='\n')
    print(f'Staged {len(checksums)} immutable files: {package}', flush=True)
    print(f'Seed documents included: {len(seeds) - 1 if seeds else 0}', flush=True)
    return package


class DigestWriter:
    def __init__(self, output):
        self.output = output
        self.digest = hashlib.sha256()

    def write(self, data):
        count = self.output.write(data)
        self.digest.update(data[:count])
        return count

    def __getattr__(self, name):
        return getattr(self.output, name)


class DigestReader:
    def __init__(self, source):
        self.source = source
        self.digest = hashlib.sha256()

    def read(self, size=-1):
        data = self.source.read(size)
        self.digest.update(data)
        return data


def archive_package(package: Path, output: Path, *, force=False) -> str:
    partial = output.with_name(output.name + '.partial')
    checksum = output.with_name(output.name + '.sha256')
    checksum_partial = checksum.with_name(checksum.name + '.partial')
    for path in (output, checksum, partial, checksum_partial):
        if path.exists() or path.is_symlink():
            if not force:
                raise ValueError(f'Output already exists; use --force to replace it: {path}')
            if path.is_symlink() or not path.is_file():
                raise ValueError(f'Output must be a regular file: {path}')
    for path in (partial, checksum_partial):
        if path.exists():
            path.unlink()
    expected = manifest_entries(package / 'SHA256SUMS')
    staged_files = {path.relative_to(package).as_posix() for path in package.rglob('*') if path.is_file()}
    if staged_files != set(expected) | {'SHA256SUMS', 'run-config.json'}:
        raise ValueError('Staged package contents differ from its checksum manifest; rebuild a fresh stage')
    epoch = int(os.environ.get('SOURCE_DATE_EPOCH', '0'))
    if epoch < 0:
        raise ValueError('SOURCE_DATE_EPOCH must not be negative')
    print(f'Writing standalone archive: {output.name}', flush=True)
    with partial.open('xb') as destination:
        writer = DigestWriter(destination)
        with tarfile.open(fileobj=writer, mode='w', format=tarfile.PAX_FORMAT,
                          dereference=True, copybufsize=CHUNK_SIZE) as archive:
            for source in [package, *sorted(package.rglob('*'))]:
                if source.is_symlink():
                    raise ValueError(f'Links are not allowed in the staged package: {source}')
                relative = source.relative_to(package)
                name = PACKAGE_NAME if source == package else f'{PACKAGE_NAME}/{relative.as_posix()}'
                info = archive.gettarinfo(str(source), arcname=name)
                info.uid = info.gid = 0
                info.uname = info.gname = ''
                info.mtime = epoch
                info.mode = 0o755 if source.is_dir() or source.suffix.lower() == '.sh' else 0o644
                if source.is_file():
                    with source.open('rb') as handle:
                        reader = DigestReader(handle)
                        archive.addfile(info, reader)
                        wanted = expected.get(relative.as_posix())
                        if wanted and reader.digest.hexdigest() != wanted:
                            raise ValueError(f'Staged file changed after verification: {relative.as_posix()}')
                elif source.is_dir():
                    archive.addfile(info)
                else:
                    raise ValueError(f'Unsupported staged filesystem entry: {source}')
        destination.flush()
        os.fsync(destination.fileno())
        digest = writer.digest.hexdigest()
    with checksum_partial.open('x', encoding='utf-8', newline='\n') as handle:
        handle.write(f'{digest}  {output.name}\n')
        handle.flush()
        os.fsync(handle.fileno())
    # The completed archive replaces the named output only after all stream hashes pass.
    os.replace(partial, output)
    os.replace(checksum_partial, checksum)
    return digest


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(newline='\n')
    project = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release-dir', type=Path, default=project / 'offline' / f'release-{VERSION}')
    parser.add_argument('--stage-root', type=Path, default=project / 'offline' / f'standalone-build-{VERSION}')
    parser.add_argument('--seed-documents', type=Path, help='Explicitly include a prepared seed directory; omitted means an empty knowledge base')
    parser.add_argument('--stage-only', action='store_true', help='Create a clean testable package directory without writing its final tar')
    parser.add_argument('--force', action='store_true', help='Replace the named final tar/checksum; never clears a nonempty stage')
    args = parser.parse_args()
    output = project / 'offline' / f'{PACKAGE_NAME}.tar'
    if not args.stage_only and not args.force:
        for path in (output, output.with_name(output.name + '.sha256'), output.with_name(output.name + '.partial'),
                     output.with_name(output.name + '.sha256.partial')):
            if path.exists() or path.is_symlink():
                parser.error(f'Output already exists; use --force: {path}')
    try:
        package = stage_package(project, args.release_dir.resolve(), args.stage_root, args.seed_documents)
        if not args.stage_only:
            digest = archive_package(package, output, force=args.force)
            print(f'Standalone package ready: {output}\nSHA256: {digest}')
    except (OSError, ValueError, tarfile.TarError) as error:
        parser.exit(1, f'Build failed: {error}\n')


if __name__ == '__main__':
    main()
