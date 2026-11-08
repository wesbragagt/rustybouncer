"""Safety checks for profiler-owned process groups and retained failure evidence."""
import json
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from benchmarks.performance_profile import Instrumented


class ProfilingSafety(unittest.TestCase):
    def test_completed_group_is_not_signaled(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Instrumented(Path(directory), Path('/unused'), mode='cpu', cpu=0)
            fixture.process = Mock()
            fixture.process.poll.return_value = 0
            with patch('os.killpg') as kill:
                fixture.stop_group()
            kill.assert_not_called()

    def test_timed_out_group_is_killed_and_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Instrumented(Path(directory), Path('/unused'), mode='cpu', cpu=0)
            fixture.process = Mock(pid=123)
            fixture.process.poll.return_value = None
            fixture.process.wait.side_effect = [subprocess.TimeoutExpired('owned',20),0]
            with patch('os.killpg') as kill, self.assertRaisesRegex(AssertionError,'did not finish cleanly'):
                fixture.stop_group()
            self.assertEqual(kill.call_args_list,[call(123,signal.SIGINT),call(123,signal.SIGKILL)])
            self.assertEqual(fixture.process.wait.call_args_list,[call(timeout=20),call(timeout=5)])

    def test_failed_resource_collection_retains_evidence_and_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Instrumented(Path(directory),Path('/unused'),mode='normalized',cpu=0)
            fixture.resource_errors = ['sampler failed']
            with self.assertRaisesRegex(AssertionError,'sampler failed'):
                fixture.finish(Path(directory),{})
            data=json.loads((Path(directory)/'resources.json').read_text())
            self.assertEqual(data['errors'],['sampler failed'])

    def test_syscall_parser_counts_resumed_calls_once(self):
        from benchmarks.performance_profile import syscall_totals
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'syscalls.txt'
            path.write_text('''100 1.0 write(4, ""..., 5 <unfinished ...>
100 1.1 <... write resumed>) = 5 <0.100000>
100 1.2 sendto(4, ""..., 8, 0, NULL, 0) = -1 EAGAIN <0.001000>
100 1.3 recvfrom(4, ""..., 8, 0, NULL, NULL) = 8 <0.002000>
''')
            totals = syscall_totals(path)
            self.assertEqual(totals['write']['calls'],1)
            self.assertEqual(totals['write']['returned_bytes'],5)
            self.assertEqual(totals['sendto']['errors'],1)
            self.assertEqual(totals['sendto']['returned_bytes'],0)
            self.assertEqual(totals['recvfrom']['returned_bytes'],8)
