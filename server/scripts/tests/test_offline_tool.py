import hashlib
import copy
from contextlib import redirect_stdout
import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

spec = importlib.util.spec_from_file_location('offline_tool', Path(__file__).parents[1] / 'offline_tool.py')
offline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(offline)


class ReleaseArtifactTests(unittest.TestCase):
    def test_recovery_preserves_compose_overlay_and_rejects_external_files(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / 'installation'
            (root / 'deploy/compose').mkdir(parents=True)
            (root / 'docker-compose.yml').write_text('services: {}\n')
            (root / 'deploy/compose/offline-validation.yml').write_text('networks: {}\n')
            env = root / '.env'
            env.write_text('COMPOSE_PATH_SEPARATOR=;\nCOMPOSE_FILE=docker-compose.yml;deploy/compose/offline-validation.yml\n')
            arguments = ['offline_tool', 'compose-files', '--root', str(root), '--env', str(env)]
            output = io.StringIO()
            with patch('sys.argv', arguments), redirect_stdout(output):
                offline.main()
            self.assertEqual(output.getvalue().splitlines(), ['docker-compose.yml', 'deploy/compose/offline-validation.yml'])
            (base / 'outside.yml').write_text('services: {}\n')
            env.write_text('COMPOSE_FILE=../outside.yml\n')
            with patch('sys.argv', arguments):
                with self.assertRaises(ValueError):
                    offline.main()

    def test_portable_image_fingerprint_ignores_engine_ids_and_rejects_content_changes(self):
        original = {
            'Id': 'sha256:oci-index', 'RepoTags': ['local-rag/backend:2.2.0'],
            'Os': 'linux', 'Architecture': 'amd64', 'RootFS': {'Layers': ['sha256:base-layer', 'sha256:app-layer']},
            'Config': {'Env': ['A=1'], 'Cmd': ['python', '-m', 'app.worker'], 'Entrypoint': None, 'WorkingDir': '/app', 'Labels': {'version': '2.2.0'}, 'ExposedPorts': {'8080/tcp': {}}},
        }
        expected = offline.image_manifest([original], ['local-rag/backend:2.2.0'])
        classic = copy.deepcopy(original)
        classic['Id'] = 'sha256:classic-config-id'
        classic['Config']['Entrypoint'] = []
        classic['Config']['Volumes'] = {}
        offline.verify_images([classic], expected)
        for field in ('layers', 'config'):
            changed = copy.deepcopy(classic)
            if field == 'layers':
                changed['RootFS']['Layers'][1] = 'sha256:altered-layer'
            else:
                changed['Config']['Env'] = ['A=2']
            with self.assertRaises(ValueError):
                offline.verify_images([changed], expected)

    def test_validates_every_model_blob_and_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            models = Path(directory)
            (models / 'blobs').mkdir()
            data = b'local-model-test'
            digest = hashlib.sha256(data).hexdigest()
            blob = models / 'blobs' / f'sha256-{digest}'
            blob.write_bytes(data)
            manifest = offline.manifest_path(models, 'demo:local')
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({'config': {'digest': f'sha256:{digest}', 'size': len(data)}, 'layers': []}))
            report = offline.verify_models(models, ['demo:local'])
            self.assertEqual(report['demo:local']['blobs'][f'blobs/sha256-{digest}'], digest)
            blob.write_bytes(b'corrupt-model---')
            with self.assertRaises(ValueError):
                offline.verify_models(models, ['demo:local'])
            blob.unlink()
            with self.assertRaises(ValueError):
                offline.verify_models(models, ['demo:local'])

    def test_archive_rejects_path_traversal_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, link in [('../outside', False), ('link', True)]:
                archive = root / 'test.tar.gz'
                with tarfile.open(archive, 'w:gz') as tar:
                    info = tarfile.TarInfo(name)
                    if link:
                        info.type = tarfile.SYMTYPE
                        info.linkname = '/etc/passwd'
                        tar.addfile(info)
                    else:
                        info.size = 4
                        tar.addfile(info, io.BytesIO(b'bad!'))
                with self.assertRaises(ValueError):
                    offline.safe_extract(archive, root / 'destination')
            self.assertFalse((root / 'outside').exists())

    def test_config_parser_does_not_evaluate_shell_substitution(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / '.env'
            env.write_text('# comment\nSECRET="$(touch /tmp/unwanted)"\nOTHER=abc\n')
            self.assertEqual(offline.read_env(env)['SECRET'], '$(touch /tmp/unwanted)')

    def test_runtime_roles_apply_only_to_new_env_and_preserve_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / '.env'
            runtime = root / 'runtime.json'
            runtime.write_text(json.dumps({'OLLAMA_CHAT_MODEL': 'custom-chat:v1', 'OLLAMA_EMBED_MODEL': 'custom-embed:v1', 'OLLAMA_VISION_MODEL': 'qwen2.5vl:3b', 'VISION_ENABLED': 'false'}))
            env.write_text('POSTGRES_PASSWORD=private-value\nOLLAMA_CHAT_MODEL=qwen3:4b-instruct\nOLLAMA_EMBED_MODEL=embeddinggemma\nOLLAMA_VISION_MODEL=qwen2.5vl:3b\nVISION_ENABLED=false\n')
            original = env.read_text()
            arguments = ['offline_tool', 'apply-runtime-config', '--runtime', str(runtime), '--env', str(env)]
            with patch('sys.argv', arguments):
                with self.assertRaises(ValueError):
                    offline.main()
            self.assertEqual(env.read_text(), original)
            with patch('sys.argv', arguments + ['--new-env']):
                offline.main()
            changed = offline.read_env(env)
            self.assertEqual(changed['OLLAMA_CHAT_MODEL'], 'custom-chat:v1')
            self.assertEqual(changed['OLLAMA_EMBED_MODEL'], 'custom-embed:v1')
            self.assertEqual(changed['POSTGRES_PASSWORD'], 'private-value')


if __name__ == '__main__':
    unittest.main()
