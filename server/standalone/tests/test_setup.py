"""Standalone configuration/install regressions using only temporary local files.

No Docker daemon, network, credentials from this checkout, or real TLS commands
are used. Prepare tests run the real standard-library release tool against
synthetic image manifests and tiny synthetic local model blobs.
"""
import hashlib
import importlib.util
import contextlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

STANDALONE = Path(__file__).resolve().parents[1]
WORKSPACE = Path(__file__).resolve().parents[3]
specification = importlib.util.spec_from_file_location('standalone_setup_under_test', STANDALONE / 'setup.py')
setup = importlib.util.module_from_spec(specification)
specification.loader.exec_module(setup)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


class TemporarySetup(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='standalone-test-')
        self.root = Path(self.temporary.name)
        self.config = self.root / 'run-config.json'
        write_json(self.config, {})

    def tearDown(self):
        self.temporary.cleanup()

    def tool(self, root=None):
        root = root or self.root
        destination = root / 'app/server/scripts/offline_tool.py'
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(WORKSPACE / 'server/scripts/offline_tool.py', destination)
        return setup.load_tool(root)

    def model_store(self, models, tool):
        for name, data in [('fixture-chat', b'local chat model fixture'), ('fixture-embed', b'local embedding fixture')]:
            sha = digest(data)
            blob = models / 'blobs' / ('sha256-' + sha)
            blob.parent.mkdir(parents=True, exist_ok=True)
            blob.write_bytes(data)
            manifest = models / 'manifests/registry.ollama.ai/library' / name / 'latest'
            write_json(manifest, {
                'schemaVersion': 2, 'config': {'digest': 'sha256:' + sha, 'size': len(data)},
                'layers': [],
            })
        return tool.verify_models(models, ['fixture-chat', 'fixture-embed'])

    def installation(self, root=None):
        root = root or self.root
        write_json(root / 'run-config.json', {'preload_documents': False})
        tool = self.tool(root)
        payload = root / 'payload'
        payload.mkdir()
        runtime = root / 'runtime'
        runtime.mkdir()
        model_source = root / 'fixture-model-source'
        report = self.model_store(model_source, tool)
        write_json(payload / 'models.json', report)
        with tarfile.open(payload / 'models.tar.gz', 'w:gz') as archive:
            archive.add(model_source, arcname='models')
        write_json(payload / 'runtime.json', {
            'OLLAMA_CHAT_MODEL': 'fixture-chat', 'OLLAMA_EMBED_MODEL': 'fixture-embed',
            'OLLAMA_VISION_MODEL': 'unused-local-vision', 'VISION_ENABLED': 'false',
        })
        inspection = [{
            'Id': 'sha256:' + str(index) * 64,
            'RepoTags': ['fixture/image-' + str(index) + ':test'],
            'Os': 'linux', 'Architecture': 'amd64',
            'RootFS': {'Layers': ['sha256:' + str(index) * 64]},
            'Config': {'Env': ['OFFLINE=true'], 'Cmd': ['local-command']},
        } for index in range(1, 8)]
        write_json(runtime / 'image-inspection.json', inspection)
        write_json(payload / 'images.json', tool.image_manifest(
            inspection, [entry['RepoTags'][0] for entry in inspection],
        ))
        (root / 'app/.env.example').write_text(
            'POSTGRES_PASSWORD=change-me\nREDIS_PASSWORD=change-me\n'
            'CHROMA_AUTH_TOKEN=change-me\nOLLAMA_CHAT_MODEL=fixture-chat\n'
            'OLLAMA_EMBED_MODEL=fixture-embed\nOLLAMA_VISION_MODEL=unused-local-vision\n'
            'VISION_ENABLED=false\nAPP_ENV=production\n',
            encoding='utf-8',
        )
        tls = root / 'app/deploy/tls'
        tls.mkdir(parents=True)
        for name in ('server.crt', 'server.key', 'ca.crt'):
            (tls / name).write_text('synthetic TLS fixture, not a real key', encoding='utf-8')
        return tool

    def prepare(self, root, host_root, tls_denied_name=None, automatic_names=None):
        real_run = subprocess.run
        calls = []

        def command(arguments, **options):
            self.assertIsInstance(arguments, list)
            self.assertNotIn('shell', options)
            calls.append(list(arguments))
            if len(arguments) > 1 and Path(arguments[1]).name == 'offline_tool.py':
                return real_run(arguments, **options)
            self.assertIn(arguments[0], {'openssl', 'bash'})
            for check in ('-checkhost', '-checkip'):
                if check in arguments and arguments[arguments.index(check) + 1] == tls_denied_name:
                    return subprocess.CompletedProcess(arguments, 1)
            return subprocess.CompletedProcess(arguments, 0)

        with patch.object(setup.subprocess, 'run', side_effect=command):
            setup.prepare(root, SimpleNamespace(host_root=host_root, server_name=automatic_names or []))
        return calls


class ConfigTests(TemporarySetup):
    def test_defaults_and_valid_explicit_settings(self):
        defaults = setup.load_config(self.root)
        self.assertEqual(defaults['https_port'], 443)
        self.assertFalse(defaults['preload_documents'])
        write_json(self.config, {
            'https_port': 9443, 'http_redirect_port': 9080,
            'web_bind_address': '127.0.0.1', 'admin_username': 'Admin.Test-1@local',
            'server_names': ['localhost', 'rag.example.local', '192.168.30.20'],
            'preload_documents': False,
        })
        loaded = setup.load_config(self.root)
        self.assertEqual(loaded['server_names'], ['localhost', 'rag.example.local', '192.168.30.20'])
        self.assertFalse(loaded['preload_documents'])

    def test_non_object_or_malformed_configuration_is_cleanly_rejected(self):
        for value in ([], None, 'not an object', 42):
            with self.subTest(value=value):
                write_json(self.config, value)
                with self.assertRaisesRegex(ValueError, 'JSON object'):
                    setup.load_config(self.root)
        self.config.write_text('{invalid JSON', encoding='utf-8')
        with self.assertRaises(ValueError):
            setup.load_config(self.root)

    def test_shell_strings_and_unknown_settings_are_rejected(self):
        invalid = [
            {'https_port': '$(touch installer-should-not-execute)'},
            {'web_bind_address': '127.0.0.1; arbitrary-command'},
            {'admin_username': 'admin && arbitrary-command'},
            {'server_names': ['$(arbitrary-command)']},
            {'server_names': ['localhost; arbitrary-command']},
            {'preload_documents': 'false; arbitrary-command'},
            {'post_install_command': 'arbitrary-command'},
        ]
        for config in invalid:
            with self.subTest(config=config):
                write_json(self.config, config)
                with self.assertRaises(ValueError):
                    setup.load_config(self.root)
        self.assertEqual(set(path.name for path in self.root.iterdir()), {'run-config.json'})

    def test_invalid_or_equal_ports_are_rejected(self):
        for field in ('https_port', 'http_redirect_port'):
            for value in (0, -1, 65536, True, 443.0, '443', None):
                with self.subTest(field=field, value=value):
                    write_json(self.config, {field: value})
                    with self.assertRaises(ValueError):
                        setup.load_config(self.root)
        write_json(self.config, {'https_port': 9443, 'http_redirect_port': 9443})
        with self.assertRaisesRegex(ValueError, 'different ports'):
            setup.load_config(self.root)

    def test_invalid_hostnames_addresses_and_admin_names_are_rejected(self):
        for name in ('host name', 'bad..name', '-bad.local', 'bad_.local', 'a' * 64 + '.local', '::1', 'user@host', '999.999.999.999'):
            with self.subTest(server_name=name):
                write_json(self.config, {'server_names': [name]})
                with self.assertRaises(ValueError):
                    setup.load_config(self.root)
        for value in ('::1', '256.0.0.1', 'localhost', None, True):
            with self.subTest(bind_address=value):
                write_json(self.config, {'web_bind_address': value})
                with self.assertRaises(ValueError):
                    setup.load_config(self.root)
        for value in ('ad', 'a' * 81, 'admin name', 'admin\n', None):
            with self.subTest(admin_username=value):
                write_json(self.config, {'admin_username': value})
                with self.assertRaises(ValueError):
                    setup.load_config(self.root)

    def test_host_paths_preserve_windows_spaces_and_reject_ambiguous_input(self):
        self.assertEqual(setup.host_path(r'C:\Users\David Smith\Offline RAG'), 'C:/Users/David Smith/Offline RAG')
        self.assertEqual(setup.host_path('/srv/Offline RAG/'), '/srv/Offline RAG')
        for value in ('relative/folder', r'C:relative\folder', '/srv/line\nbreak', '/srv/null\0byte', '/srv/"quoted"', "/srv/'quoted'", '/', 'C:/'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    setup.host_path(value)


class InstallationTests(TemporarySetup):
    def test_repeated_prepare_preserves_credentials_project_and_windows_paths(self):
        tool = self.installation()
        self.prepare(self.root, r'C:\Users\David Smith\Offline RAG')
        first = tool.read_env(self.root / 'app/.env')
        keys = ('POSTGRES_PASSWORD', 'REDIS_PASSWORD', 'CHROMA_AUTH_TOKEN', 'COMPOSE_PROJECT_NAME')
        original = {key: first[key] for key in keys}
        self.assertEqual(len({first[key] for key in keys[:3]}), 3)
        self.assertTrue(all(len(first[key]) >= 24 for key in keys[:3]))
        self.assertTrue(first['COMPOSE_PROJECT_NAME'].startswith('local-rag-standalone-'))
        self.assertEqual(first['HOST_DATA_PATH'], 'C:/Users/David Smith/Offline RAG/app/data')
        write_json(self.config, {'https_port': 9443, 'http_redirect_port': 9080, 'preload_documents': False})
        self.prepare(self.root, r'D:\Offline Applications\RAG Server')
        second = tool.read_env(self.root / 'app/.env')
        self.assertEqual({key: second[key] for key in keys}, original)
        self.assertEqual(second['HOST_DATA_PATH'], 'D:/Offline Applications/RAG Server/app/data')
        self.assertEqual(second['OLLAMA_HOST_PATH'], 'D:/Offline Applications/RAG Server/app/data/ollama')
        self.assertEqual(second['HTTPS_PORT'], '9443')
        self.assertEqual(second['HTTP_REDIRECT_PORT'], '9080')
        self.assertEqual(second['LITERATURE_SITE_URL'], '')
        self.assertEqual(second['COOKIE_SECURE'], 'true')
        self.assertEqual(second['COMPOSE_FILE'], 'docker-compose.yml:deploy/compose/offline-validation.yml')
        lines = (self.root / 'app/.env').read_text().splitlines()
        for key in (*keys, 'HOST_DATA_PATH', 'HTTPS_PORT'):
            self.assertEqual(sum(line.startswith(key + '=') for line in lines), 1)
        marker = setup.read_json(self.root / 'runtime/install.json')
        self.assertEqual(marker['project'], original['COMPOSE_PROJECT_NAME'])
        self.assertEqual(marker['host_root'], 'D:/Offline Applications/RAG Server')
        self.assertFalse((self.root / 'seed-documents').exists())

    def test_separate_installations_receive_unique_credentials_and_projects(self):
        first = self.root / 'installation one'
        second = self.root / 'installation two'
        first.mkdir()
        second.mkdir()
        tool = self.installation(first)
        self.installation(second)
        self.prepare(first, r'C:\Offline RAG one')
        self.prepare(second, r'C:\Offline RAG two')
        one, two = tool.read_env(first / 'app/.env'), tool.read_env(second / 'app/.env')
        for key in ('POSTGRES_PASSWORD', 'REDIS_PASSWORD', 'CHROMA_AUTH_TOKEN', 'COMPOSE_PROJECT_NAME'):
            self.assertNotEqual(one[key], two[key])

    def test_retained_certificate_must_cover_new_explicit_server_name(self):
        self.installation()
        self.prepare(self.root, r'C:\Offline RAG')
        marker = (self.root / 'runtime/install.json').read_bytes()
        private_key = (self.root / 'app/deploy/tls/server.key').read_bytes()
        write_json(self.config, {'server_names': ['uncovered.example.local'], 'preload_documents': False})
        with self.assertRaisesRegex(ValueError, 'certificate does not cover'):
            self.prepare(self.root, r'C:\Offline RAG', tls_denied_name='uncovered.example.local')
        self.assertEqual((self.root / 'runtime/install.json').read_bytes(), marker)
        self.assertEqual((self.root / 'app/deploy/tls/server.key').read_bytes(), private_key)

    def test_certificate_covers_explicit_names_without_requiring_transient_ips(self):
        self.installation()
        write_json(self.config, {
            'server_names': ['rag.example.local', '192.168.30.20'], 'preload_documents': False,
        })
        calls = self.prepare(
            self.root, r'C:\Offline RAG', tls_denied_name='192.168.99.10',
            automatic_names=['192.168.99.10'],
        )
        checked = set()
        for command in calls:
            for flag in ('-checkhost', '-checkip'):
                if flag in command:
                    checked.add(command[command.index(flag) + 1])
        self.assertEqual(checked, {'localhost', '127.0.0.1', 'rag.example.local', '192.168.30.20'})

    def test_environment_rewrite_preserves_unrelated_settings_comments_and_credentials(self):
        path = self.root / '.env'
        contents = (
            '# Existing installation\nPOSTGRES_PASSWORD=retained-test-value\n'
            'COMPOSE_PROJECT_NAME=retained-project\nAPP_ENV=production\nHTTPS_PORT=443\n'
        )
        path.write_text(contents, encoding='utf-8')
        for port in ('8443', '9443'):
            setup.update_env(path, {
                'HTTPS_PORT': port, 'HOST_DATA_PATH': "'C:/Users/David Smith/RAG/app/data'",
            })
        result = path.read_text()
        self.assertIn('# Existing installation\n', result)
        self.assertIn('POSTGRES_PASSWORD=retained-test-value\n', result)
        self.assertIn('COMPOSE_PROJECT_NAME=retained-project\n', result)
        self.assertIn('APP_ENV=production\n', result)
        self.assertIn("HOST_DATA_PATH='C:/Users/David Smith/RAG/app/data'\n", result)
        self.assertEqual(result.count('HTTPS_PORT='), 1)


class CachedModelsAndSeedsTests(TemporarySetup):
    def test_cached_models_require_manifests_all_blobs_and_exact_sizes(self):
        tool = self.tool()
        models = self.root / 'models'
        report = self.model_store(models, tool)
        self.assertTrue(setup.model_files_present(tool, models, report))
        entry = report['fixture-chat']
        manifest = models / entry['manifest']
        original = manifest.read_bytes()
        manifest.unlink()
        self.assertFalse(setup.model_files_present(tool, models, report))
        manifest.write_bytes(original)
        blob = models / next(iter(entry['blobs']))
        data = blob.read_bytes()
        blob.unlink()
        self.assertFalse(setup.model_files_present(tool, models, report))
        blob.write_bytes(data + b'extra')
        self.assertFalse(setup.model_files_present(tool, models, report))
        blob.write_bytes(data)
        self.assertTrue(setup.model_files_present(tool, models, report))

    def test_altered_model_manifest_invalidates_cache_even_with_existing_blobs(self):
        tool = self.tool()
        models = self.root / 'models'
        report = self.model_store(models, tool)
        manifest = models / report['fixture-chat']['manifest']
        value = setup.read_json(manifest)
        value['unexpected'] = 'modified after install'
        write_json(manifest, value)
        self.assertFalse(setup.model_files_present(tool, models, report))

    def seed(self, filename='manual.docx', expected=None):
        root = self.root / 'seed-documents'
        root.mkdir(exist_ok=True)
        contents = b'synthetic seed document fixture'
        if '/' not in filename and '\\' not in filename:
            (root / filename).write_bytes(contents)
        entry = {
            'filename': filename, 'sha256': expected or digest(contents),
            'origin': 'admin', 'version_rank': 1, 'source_name': 'manual_v1.docx',
            'topic_key': 'admin/manual', 'version_label': 'v1',
        }
        write_json(root / 'documents.json', {'documents': [entry]})
        return entry

    def test_empty_installation_and_valid_synthetic_seed_metadata(self):
        tool = self.tool()
        self.assertEqual(setup.seed_entries(self.root, tool), [])
        entry = self.seed()
        self.assertEqual(setup.seed_entries(self.root, tool), [entry])

    def test_seed_path_traversal_and_absolute_names_are_rejected(self):
        tool = self.tool()
        for name in ('../manual.docx', 'folder/manual.docx', r'folder\manual.docx', '/manual.docx', r'C:\outside\manual.docx'):
            with self.subTest(filename=name):
                self.seed(name)
                with self.assertRaisesRegex(ValueError, 'filename'):
                    setup.seed_entries(self.root, tool)

    def test_seed_bad_hash_or_non_document_extension_is_rejected(self):
        tool = self.tool()
        for expected in ('a' * 64, 'a' * 63, 'G' * 64, 'not-a-digest'):
            with self.subTest(digest=expected):
                self.seed(expected=expected)
                with self.assertRaises(ValueError):
                    setup.seed_entries(self.root, tool)
        self.seed('manual.exe')
        with self.assertRaisesRegex(ValueError, 'filename'):
            setup.seed_entries(self.root, tool)

    def test_seed_symlinks_are_rejected(self):
        tool = self.tool()
        self.seed()
        path = self.root / 'seed-documents/manual.docx'
        outside = self.root / 'outside.docx'
        path.rename(outside)
        try:
            path.symlink_to(outside)
        except OSError:
            self.skipTest('Symlink creation is unavailable in this host environment')
        with self.assertRaisesRegex(ValueError, 'path'):
            setup.seed_entries(self.root, tool)

    def test_malformed_seed_metadata_and_boolean_version_are_cleanly_rejected(self):
        tool = self.tool()
        entry = self.seed()
        manifest = self.root / 'seed-documents/documents.json'
        for value in ([], None, {'documents': None}, {'documents': ['not an entry']}, {'documents': [{'filename': None}]}):
            with self.subTest(value=value):
                write_json(manifest, value)
                with self.assertRaises(ValueError):
                    setup.seed_entries(self.root, tool)
        write_json(manifest, {'documents': [{**entry, 'version_rank': True}]})
        with self.assertRaisesRegex(ValueError, 'identity'):
            setup.seed_entries(self.root, tool)


class ProvisionDisplayTests(TemporarySetup):
    def test_stale_saved_password_is_redacted_without_changing_existing_account(self):
        self.tool()
        write_json(self.config, {'preload_documents': False})
        saved = self.root / 'runtime/admin-login.txt'
        saved.parent.mkdir()
        saved.write_text('Username: admin\nPassword: old-fixture-password\n', encoding='utf-8')
        existing = SimpleNamespace(role='admin', active=True, password_hash='current-fixture-hash')
        added = []

        @contextlib.contextmanager
        def database():
            yield SimpleNamespace(scalar=lambda statement: existing, add=added.append)

        class Query:
            def where(self, *conditions):
                return self

        modules = {
            'app': types.ModuleType('app'),
            'sqlalchemy': types.ModuleType('sqlalchemy'),
            'app.config': types.ModuleType('app.config'),
            'app.db': types.ModuleType('app.db'),
            'app.ingestion': types.ModuleType('app.ingestion'),
            'app.security': types.ModuleType('app.security'),
        }
        modules['app'].__path__ = []
        modules['sqlalchemy'].select = lambda model: Query()
        modules['app.config'].get_settings = lambda: SimpleNamespace(data_path=self.root / 'app/data')
        modules['app.db'].User = SimpleNamespace(username='admin')
        modules['app.db'].db_session = database
        modules['app.security'].hash_password = lambda value: 'new-hash'
        modules['app.security'].verify_password = lambda password, hashed: False

        def no_ingestion(*arguments, **options):
            raise AssertionError('The default empty knowledge base must not ingest documents')

        modules['app.ingestion'].ingest_path = no_ingestion
        output = io.StringIO()
        with patch.dict(setup.sys.modules, modules), contextlib.redirect_stdout(output):
            setup.provision(self.root)
            setup.show(self.root)
        self.assertEqual(existing.password_hash, 'current-fixture-hash')
        self.assertEqual(added, [])
        self.assertNotIn('old-fixture-password', saved.read_text())
        self.assertNotIn('old-fixture-password', output.getvalue())
        self.assertIn('use your current password', saved.read_text())
        self.assertIn('admin', output.getvalue())

class AtomicWriteTests(TemporarySetup):
    def test_replace_failure_retains_original_and_removes_temporary_file(self):
        path = self.root / '.env'
        path.write_text('original retained configuration\n', encoding='utf-8')
        with patch.object(setup.os, 'replace', side_effect=PermissionError('injected rename failure')):
            with self.assertRaises(PermissionError):
                setup.atomic_write(path, 'replacement configuration\n')
        self.assertEqual(path.read_text(), 'original retained configuration\n')
        self.assertEqual(set(item.name for item in self.root.iterdir()), {'.env', 'run-config.json'})

    def test_chmod_failure_retains_original_and_removes_temporary_file(self):
        path = self.root / 'install.json'
        path.write_text('original marker\n', encoding='utf-8')
        with patch.object(Path, 'chmod', side_effect=PermissionError('injected permission failure')):
            with self.assertRaises(PermissionError):
                setup.atomic_write(path, 'new marker\n')
        self.assertEqual(path.read_text(), 'original marker\n')
        self.assertFalse(list(self.root.glob('*.tmp')))

    def test_partial_write_failure_retains_original_and_cleans_partial_data(self):
        path = self.root / '.env'
        path.write_text('original retained credentials\n', encoding='utf-8')
        real_open = Path.open

        class InterruptedWriter:
            def __init__(self, file):
                self.file = file

            def __enter__(self):
                self.file.__enter__()
                return self

            def write(self, value):
                self.file.write(value[:4])
                self.file.flush()
                raise OSError('injected full disk during write')

            def __exit__(self, *arguments):
                return self.file.__exit__(*arguments)

        def open_file(filename, *arguments, **options):
            file = real_open(filename, *arguments, **options)
            return InterruptedWriter(file) if filename.name.endswith('.tmp') else file

        with patch.object(Path, 'open', open_file):
            with self.assertRaisesRegex(OSError, 'full disk'):
                setup.atomic_write(path, 'partially written new credentials\n')
        self.assertEqual(path.read_text(), 'original retained credentials\n')
        self.assertFalse(list(self.root.glob('*.tmp')))

    def test_json_write_is_utf8_atomic_and_private_on_posix(self):
        path = self.root / 'runtime/install.json'
        setup.write_json(path, {'path': 'C:/Offline Apps/שרת'})
        self.assertEqual(setup.read_json(path), {'path': 'C:/Offline Apps/שרת'})
        self.assertNotIn(b'\r\n', path.read_bytes())
        if os.name == 'posix':
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(path.parent.glob('*.tmp')))


if __name__ == '__main__':
    unittest.main()
