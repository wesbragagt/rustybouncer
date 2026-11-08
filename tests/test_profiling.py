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


class ProfileReport(unittest.TestCase):
    def test_matrix_rejects_duplicate_replacing_required_arm(self):
        from benchmarks.profile_report import matrix
        rows = [{'clients': c, 'repetition': r, 'target': t} for c in (10,100)
                for r in (1,2,3) for t in ('direct','pgbouncer-session','pgbouncer-transaction',
                                           'rustybouncer-session','rustybouncer-routed')]
        rows[-1] = rows[0]
        with self.assertRaisesRegex(AssertionError,'incomplete or duplicate'):
            matrix(rows)

    def test_switch_intervals_are_thread_local_clipped_and_not_wall_percentage(self):
        from benchmarks.profile_report import switches
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'stacks.txt'
            path.write_text("""worker 10 1.000000: PERF_RECORD_SWITCH OUT
worker 11 1.500000: PERF_RECORD_SWITCH OUT
worker 10 2.000000: PERF_RECORD_SWITCH IN
worker 11 3.000000: PERF_RECORD_SWITCH IN
worker 12 3.500000: PERF_RECORD_SWITCH IN
worker 13 3.600000: PERF_RECORD_SWITCH OUT
""")
            result=switches(path,1.75,2.5)
            self.assertEqual(result['matched_in_window'],2)
            self.assertEqual(result['aggregate_thread_offcpu_seconds'],1.0)
            self.assertEqual(result['wall_seconds'],.75)
            self.assertEqual(result['unmatched_in'],1)
            self.assertEqual(result['unmatched_out'],1)


class WorkloadEvidence(unittest.TestCase):
    def test_mixed_latency_keeps_short_and_long_distributions(self):
        from benchmarks.workload_compare import latencies
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'latency.1').write_text('0 1 100 0 1 1\n1 1 20000 1 1 1\n')
            result=latencies(root,'mixed')
            self.assertEqual(result['scripts']['0']['p99_ms'],.1)
            self.assertEqual(result['scripts']['1']['p99_ms'],20)
            self.assertEqual(result['all']['samples'],2)

    def test_missing_long_workload_cannot_pass(self):
        from benchmarks.workload_compare import latencies
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'latency.1').write_text('0 1 100 0 1 1\n')
            with self.assertRaisesRegex(AssertionError,'missing workload'):
                latencies(root,'mixed')


class HeapEvidence(unittest.TestCase):
    def test_bytes_count_allocations_not_live_or_peak_size(self):
        from benchmarks.profile_report import heap_summary
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'sizes.tsv'
            path.write_text('16\t100\n32\t10\n')
            result=heap_summary(path,10)
            self.assertEqual(result['allocation_calls'],110)
            self.assertEqual(result['allocated_bytes_including_reallocations'],1920)
            self.assertEqual(result['calls_per_controlled_q'],11)


class CpuEvidence(unittest.TestCase):
    def test_samples_are_period_weighted_and_unresolved_work_is_retained(self):
        from benchmarks.profile_report import sample_categories
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'stacks.txt'
            path.write_text("""worker 10 1.0: 100 cpu-clock:u:
  111 rustybouncer::router::scan_words (binary)
  222 rustybouncer::router::route_query (binary)

worker 10 2.0: PERF_RECORD_SWITCH OUT
worker 11 2.5: 300 cpu-clock:u:
  333 unresolved_symbol (lib)
""")
            result=sample_categories(path)
            self.assertEqual(result['samples'],2)
            self.assertEqual(result['period_weight'],400)
            self.assertEqual(result['ranked_categories'][0]['category'],'runtime_or_other')
            self.assertEqual(result['ranked_categories'][0]['period_share'],.75)


class AttributionSymbols(unittest.TestCase):
    def test_renamed_wire_and_classifier_helpers_retain_explicit_attribution(self):
        from benchmarks.profile_report import category
        self.assertEqual(category('rustybouncer::router::wire::send\nrustybouncer::router::route_query'),'frame_write')
        self.assertEqual(category('rustybouncer::router::wire::response\nrustybouncer::router::route_query'),'response_read_and_assembly')
        self.assertEqual(category('rustybouncer::router::classification::scan'),'classification')


class ProfilerExitStatus(unittest.TestCase):
    def test_failed_owned_profiler_exit_cannot_be_counted_as_success(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture=Instrumented(Path(directory),Path('/unused'),mode='heap',cpu=0)
            fixture.process=Mock()
            fixture.process.poll.return_value=7
            with self.assertRaisesRegex(AssertionError,'status 7'):
                fixture.stop_group()
