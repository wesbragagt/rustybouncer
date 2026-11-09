"""Failure-path checks for benchmark resource ownership and retained diagnostics."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from benchmarks import metrics as benchmark


class BenchmarkLifecycle(unittest.TestCase):
    def test_partial_preflight_connect_failure_closes_every_created_client(self):
        one, two = Mock(), Mock()
        cluster = SimpleNamespace(connect=Mock(side_effect=[one, OSError('connect failed'), two]))
        with self.assertRaisesRegex(OSError, 'connect failed'):
            benchmark.preflight(cluster, 3)
        one.close.assert_called_once()
        two.close.assert_called_once()

    def test_preflight_query_failure_closes_all_clients(self):
        one, two = Mock(), Mock()
        one.query.side_effect = OSError('query failed')
        two.query.return_value = [['1']]
        cluster = SimpleNamespace(connect=Mock(side_effect=[one, two]))
        with self.assertRaisesRegex(OSError, 'query failed'):
            benchmark.preflight(cluster, 2)
        one.close.assert_called_once()
        two.close.assert_called_once()

    def test_timed_out_pgbench_keeps_partial_output(self):
        failure = subprocess.TimeoutExpired('pgbench', 60, output=b'partial stdout', stderr=b'partial stderr')
        with tempfile.TemporaryDirectory() as directory, patch.object(benchmark.subprocess, 'run', side_effect=failure):
            path = Path(directory)
            with self.assertRaises(subprocess.TimeoutExpired):
                benchmark.pgbench(SimpleNamespace(proxy_port=6432), 100, 30, path, True)
            log = (path / 'pgbench.txt').read_text()
            self.assertIn('partial stdout', log)
            self.assertIn('partial stderr', log)
            self.assertIn('operation deadline', log)
