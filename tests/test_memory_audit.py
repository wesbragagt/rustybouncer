"""The allocation acceptance checker must reject incomplete or unsafe evidence."""
import tempfile
import unittest
from pathlib import Path

from benchmarks.memory_audit import inspect


class MemoryAudit(unittest.TestCase):
    def report(self, body):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'memcheck.xml'
            path.write_text('<valgrindoutput>' + body + '</valgrindoutput>')
            return inspect(path)

    def test_incomplete_report_is_never_accepted(self):
        with self.assertRaisesRegex(AssertionError, 'incomplete'):
            self.report('<status><state>RUNNING</state></status>')

    def test_invalid_access_and_lost_allocations_are_rejected(self):
        for kind in ('InvalidRead', 'InvalidWrite', 'Leak_DefinitelyLost',
                     'Leak_IndirectlyLost', 'Leak_PossiblyLost'):
            with self.subTest(kind=kind), self.assertRaisesRegex(AssertionError, 'Memcheck errors'):
                self.report('<status><state>FINISHED</state></status><error><kind>' + kind + '</kind></error>')

    def test_even_reachable_private_response_storage_is_rejected(self):
        with self.assertRaisesRegex(AssertionError, 'retained private response storage'):
            self.report('''<status><state>FINISHED</state></status>
<error><kind>Leak_StillReachable</kind><xwhat><leakedbytes>65536</leakedbytes>
<leakedblocks>1</leakedblocks></xwhat><stack><frame><fn>router::wire::response</fn>
<file>wire.rs</file><dir>/source/src/router</dir></frame></stack></error>''')

    def test_runtime_globals_remain_visible(self):
        result = self.report('''<status><state>FINISHED</state></status>
<error><kind>Leak_StillReachable</kind><xwhat><leakedbytes>1024</leakedbytes>
<leakedblocks>2</leakedblocks></xwhat><stack><frame><fn>runtime_global</fn></frame></stack></error>''')
        self.assertEqual(result['other_still_reachable_bytes'], 1024)
        self.assertEqual(result['other_still_reachable_blocks'], 2)
        self.assertEqual(result['private_response_findings'], 0)
