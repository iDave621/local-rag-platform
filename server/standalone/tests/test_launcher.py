"""Launcher orchestration with a fake Docker executable; never contacts a daemon."""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
root = Path(os.environ['FIXTURE_PACKAGE'])
args = sys.argv[1:]
with (root / 'commands.jsonl').open('a') as output:
    output.write(json.dumps(args) + '\n')
if 'build' in args or 'pull' in args:
    raise SystemExit('Unexpected image build/pull')
if args[:2] == ['info', '--format']:
    print('linux ' + os.environ.get('FIXTURE_ARCH', 'amd64'))
elif args[:3] == ['compose', 'version', '--short']:
    print(os.environ.get('FIXTURE_COMPOSE', '2.24.4'))
elif args[:2] == ['image', 'inspect']:
    if not (root / 'images-loaded').exists():
        raise SystemExit(1)
    assert len(args[2:]) == 7
    print('[]')
elif args[:2] == ['load', '--input']:
    assert args[2] == str(root / 'payload/images.tar.gz')
    (root / 'images-loaded').touch()
elif args and args[0] == 'run':
    assert '--network' in args and args[args.index('--network') + 1] == 'none'
    assert '--pull' in args and args[args.index('--pull') + 1] == 'never'
    assert args[args.index('--mount') + 1] == f'type=bind,source={root},target=/package'
    action = args[args.index('/package/setup.py') + 1]
    if action == 'prepare':
        if os.environ.get('FIXTURE_FAIL_PREPARE') == '1':
            raise SystemExit('Fixture prepare failure')
        assert args[args.index('--host-root') + 1] == str(root)
        (root / 'runtime/install.json').write_text('{}')
        (root / 'app').mkdir(exist_ok=True)
    elif action == 'show':
        print('Fixture server ready')
    else:
        raise SystemExit('Unexpected helper action')
elif args[:2] == ['compose', 'up']:
    assert '--no-build' in args and '--wait' in args
    assert args[args.index('--pull') + 1] == 'never'
    if os.environ.get('FIXTURE_FAIL_START') == '1':
        raise SystemExit('Fixture health check failure')
elif args[:2] == ['compose', 'run']:
    assert '--rm' in args and '--no-deps' in args and '-T' in args
    assert args[args.index('-v') + 1] == f'{root}:/package'
    assert args[-4:] == ['api', 'python', '/package/setup.py', 'provision']
elif args in (['compose', 'ps', '-a'], ['compose', 'stop']):
    pass
else:
    raise SystemExit('Unexpected Docker arguments: ' + repr(args))
'''


@unittest.skipIf(os.name == 'nt', 'Linux launcher mocks require a POSIX shell; Windows uses RUN.ps1')
class StandaloneLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='standalone launch ')
        self.root = Path(self.temporary.name) / 'package with spaces'
        (self.root / 'payload').mkdir(parents=True)
        (self.root / 'bin').mkdir()
        shutil.copyfile(Path(__file__).parents[1] / 'RUN.sh', self.root / 'RUN.sh')
        (self.root / 'setup.py').write_text('# fixture\n')
        (self.root / 'payload/images.tar.gz').write_bytes(b'fixture images')
        (self.root / 'payload/models.tar.gz').write_bytes(b'fixture models')
        names = ['setup.py', 'payload/images.tar.gz', 'payload/models.tar.gz']
        (self.root / 'SHA256SUMS').write_text(''.join(f'{hashlib.sha256((self.root / name).read_bytes()).hexdigest()}  {name}\n' for name in names))
        docker = self.root / 'bin/docker'
        docker.write_text(FAKE_DOCKER)
        docker.chmod(0o755)
        self.env = dict(os.environ, FIXTURE_PACKAGE=str(self.root), PATH=str(self.root / 'bin') + os.pathsep + os.environ['PATH'])

    def tearDown(self):
        self.temporary.cleanup()

    def run_action(self, action, **extra):
        return subprocess.run(['bash', str(self.root / 'RUN.sh'), action], env=dict(self.env, **extra), text=True, capture_output=True, timeout=15)

    def commands(self):
        return [json.loads(line) for line in (self.root / 'commands.jsonl').read_text().splitlines()]

    def test_first_install_repeat_start_status_and_stop_without_downloads(self):
        for action in ('start', 'start', 'install', 'status', 'stop'):
            result = self.run_action(action)
            self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.commands()
        self.assertEqual(sum(command[0] == 'load' for command in commands), 1)
        self.assertEqual(sum(command[:2] == ['compose', 'up'] for command in commands), 3)
        self.assertIn(['compose', 'ps', '-a'], commands)
        self.assertIn(['compose', 'stop'], commands)

    def test_invalid_checksum_never_loads_or_starts_images(self):
        (self.root / 'payload/images.tar.gz').write_bytes(b'corrupt fixture')
        result = self.run_action('start')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(command[0] in ('load', 'run') or command[:2] == ['compose', 'up'] for command in self.commands()))

    def test_prepare_failure_stops_before_service_start(self):
        result = self.run_action('start', FIXTURE_FAIL_PREPARE='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'runtime/install.json').exists())
        self.assertFalse(any(command[:2] == ['compose', 'up'] for command in self.commands()))

    def test_wrong_engine_architecture_never_loads_images(self):
        result = self.run_action('start', FIXTURE_ARCH='arm64')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(command[0] == 'load' for command in self.commands()))

    def test_old_compose_never_loads_images(self):
        result = self.run_action('start', FIXTURE_COMPOSE='2.24.3')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(command[0] == 'load' for command in self.commands()))

    def test_failed_start_never_provisions_or_reports_ready(self):
        result = self.run_action('start', FIXTURE_FAIL_START='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(command[:2] == ['compose', 'run'] for command in self.commands()))
        self.assertNotIn('The server is ready.', result.stdout)


if __name__ == '__main__':
    unittest.main()
