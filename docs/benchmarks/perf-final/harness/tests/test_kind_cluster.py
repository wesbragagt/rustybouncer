"""Safety regressions for fault injection and evidence ownership."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests import kind_cnpg
from tests.kind_cluster import KindCluster


class KindSafety(unittest.TestCase):
    def fixture(self, directory):
        return KindCluster(Path(directory) / 'new-run', 'rustybouncer:latest')

    def test_fault_refuses_an_unowned_or_control_plane_node(self):
        with tempfile.TemporaryDirectory() as directory:
            cluster = self.fixture(directory)
            cluster.primary = Mock(return_value='postgres-1')
            cluster.run = Mock()
            for node in ('production-worker', cluster.name + '-control-plane'):
                cluster.pods = Mock(return_value=[{'metadata': {'name': 'postgres-1'}, 'spec': {'nodeName': node}}])
                with self.assertRaisesRegex(AssertionError, 'refusing to crash'):
                    cluster.crash_primary()
                self.assertEqual(cluster.stopped, set())
            cluster.run.assert_not_called()

    def test_failed_restart_still_attempts_cluster_deletion_and_removes_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            cluster = self.fixture(directory)
            cluster.created = True
            node = cluster.name + '-worker'
            cluster.stopped.add(node)
            cluster.kubeconfig.write_text('private fixture credential')
            def command(*args, **kwargs):
                if args[:2] == ('docker', 'start'):
                    raise RuntimeError('restart failed')
                return ''
            cluster.run = Mock(side_effect=command)
            cluster.diagnostics = Mock()
            with self.assertRaisesRegex(ExceptionGroup, 'cleanup failures'):
                cluster.close()
            cluster.run.assert_any_call('kind', 'delete', 'cluster', '--name', cluster.name, '--kubeconfig', str(cluster.kubeconfig), seconds=120)
            self.assertFalse(cluster.kubeconfig.exists())

    def test_existing_evidence_directory_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'results.json'
            marker.write_text('previous evidence')
            with patch('sys.argv', ['kind-cnpg', '--output', directory]), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    kind_cnpg.main()
            self.assertEqual(error.exception.code, 2)
            self.assertEqual(marker.read_text(), 'previous evidence')
