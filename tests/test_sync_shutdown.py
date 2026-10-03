"""Real signal delivery verifies that sync stops cleanly without losing heartbeat."""
import os
import signal
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


@unittest.skipUnless(os.name == 'posix', 'POSIX signal acceptance runs in the release container')
class SyncShutdownTests(unittest.TestCase):
    def process(self, active=False):
        code = textwrap.dedent("""
            import time
            from types import SimpleNamespace
            from app import sync_worker
            sync_worker.touch_worker = lambda *a, **kw: print('HEARTBEAT', flush=True)
            sync_worker.retire_worker = lambda *a, **kw: print('RETIRED', flush=True)
            sync_worker.get_settings = lambda: SimpleNamespace(
                literature_site_url='http://internal/' if ACTIVE else '',
                job_heartbeat_seconds=.03, literature_sync_interval_seconds=60)
            class FakeSync:
                def run_once(self):
                    print('ACTIVE', flush=True)
                    time.sleep(2.2)
                    print('FINISHED', flush=True)
                    return {'queued': 0}
            sync_worker.LiteratureSync = FakeSync
            sync_worker.main()
        """)
        code = f'ACTIVE = {active!r}\n' + code
        environment = dict(os.environ)
        environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'backend')
        environment['DATABASE_URL'] = 'sqlite://'
        return subprocess.Popen(
            [sys.executable, '-u', '-c', code], env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def test_sigterm_finishes_active_sync_and_keeps_heartbeat_until_done(self):
        process = self.process(active=True)
        seen = []
        try:
            while True:
                line = process.stdout.readline()
                self.assertTrue(line, 'Sync process exited before becoming active')
                seen.append(line)
                if 'ACTIVE' in line:
                    break
            process.send_signal(signal.SIGTERM)
            output, errors = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, errors)
            self.assertIn('FINISHED', output)
            self.assertIn('RETIRED', output)
            self.assertGreaterEqual(output.count('HEARTBEAT'), 2)
            self.assertEqual((''.join(seen) + output).count('ACTIVE'), 1)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_sigint_interrupts_idle_interval(self):
        process = self.process()
        try:
            while True:
                line = process.stdout.readline()
                self.assertTrue(line, 'Sync process exited before its heartbeat')
                if line.strip() == 'HEARTBEAT':
                    break
            process.send_signal(signal.SIGINT)
            output, errors = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, errors)
            self.assertIn('RETIRED', output)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


if __name__ == '__main__':
    unittest.main()
