"""H12/R12 complete offline reporter integration; simulated bad runs are inputs."""
import io
import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from conformance import __main__ as cli
from conformance.reporting import Result


class Reporting(unittest.TestCase):
    def run_cases(self, *cases):
        result = Result(io.StringIO(), True, 0)
        unittest.TestSuite(cases).run(result)
        return result

    def reports(self, result, contract=None):
        with tempfile.TemporaryDirectory() as directory:
            jp, xp = Path(directory) / 'out.json', Path(directory) / 'out.xml'
            result.write(jp, xp, contract or {})
            return json.loads(jp.read_text()), ET.parse(xp).getroot(), jp.read_text() + xp.read_text()

    def test_repeated_same_instance_has_independent_final_duration_and_evidence(self):
        class Sample(unittest.TestCase):
            def runTest(self):
                self.evidence = ['body']
                self.addCleanup(lambda: self.evidence.append('cleanup'))
        case = Sample()
        with patch('conformance.reporting.time.monotonic', side_effect=[1, 4, 8, 13]):
            result = self.run_cases(case, case)
        self.assertEqual([row['duration_s'] for row in result.records], [3, 5])
        self.assertEqual([row['evidence'] for row in result.records], [['body', 'cleanup']] * 2)
        self.assertTrue(result.wasSuccessful())
        data, xml, _ = self.reports(result)
        self.assertTrue(data['accepted'])
        self.assertEqual(xml.attrib['tests'], '2')

    def test_body_and_multiple_cleanup_errors_aggregate(self):
        class Sample(unittest.TestCase):
            def runTest(self):
                def bad(message):
                    raise RuntimeError(message)
                self.addCleanup(bad, 'cleanup-one')
                self.addCleanup(bad, 'cleanup-two')
                self.fail('body-failure')
        result = self.run_cases(Sample())
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0]['status'], 'error')
        for message in ('cleanup-one', 'cleanup-two', 'body-failure'):
            self.assertIn(message, result.records[0]['detail'])
        data, xml, _ = self.reports(result)
        self.assertEqual(data['summary']['error'], 1)
        self.assertEqual(xml.attrib['errors'], '1')
        self.assertEqual(xml.attrib['failures'], '0')
        self.assertFalse(data['accepted'])

    def test_setup_and_teardown_errors(self):
        class Setup(unittest.TestCase):
            def setUp(self):
                self.addCleanup(lambda: self.fail('cleanup'))
                raise RuntimeError('setup')
            def runTest(self):
                raise AssertionError('unreachable')
        class Teardown(unittest.TestCase):
            def runTest(self):
                pass
            def tearDown(self):
                raise RuntimeError('teardown')
        result = self.run_cases(Setup(), Teardown())
        self.assertEqual([row['status'] for row in result.records], ['error', 'error'])
        self.assertIn('cleanup', result.records[0]['detail'])
        self.assertFalse(result.wasSuccessful())

    def test_fixture_errors_without_start_event(self):
        class Sample(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise RuntimeError('class setup')
            def runTest(self):
                pass
        result = self.run_cases(Sample())
        self.assertEqual(result.testsRun, 0)
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0]['status'], 'error')
        self.assertFalse(result.wasSuccessful())

    def test_subtests_aggregate_failures_errors_and_skips(self):
        class Sample(unittest.TestCase):
            def runTest(self):
                with self.subTest(part='failure'):
                    self.fail('subfailure')
                with self.subTest(part='error'):
                    raise RuntimeError('suberror')
                with self.subTest(part='skip'):
                    self.skipTest('subskip')
        result = self.run_cases(Sample())
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0]['status'], 'error')
        for message in ('subfailure', 'suberror', 'subskip'):
            self.assertIn(message, result.records[0]['detail'])
        self.assertEqual(len(result.skipped), 1)
        data, _, _ = self.reports(result)
        self.assertEqual(data['diagnostic_counts'],
                         {'failed': 1, 'error': 1, 'skipped': 1,
                          'expected_failure': 0, 'unexpected_success': 0})
        self.assertFalse(result.wasSuccessful())

    def test_all_outcomes_and_junit_totals(self):
        def make(status):
            def body(case):
                if status == 'failed':
                    case.fail('failure')
                if status == 'error':
                    raise RuntimeError('error')
                if status == 'skipped':
                    case.skipTest('skip')
                if status == 'expected_failure':
                    case.fail('expected')
            if status in ('expected_failure', 'unexpected_success'):
                body = unittest.expectedFailure(body)
            return type('Sample', (unittest.TestCase,), {'runTest': body})()
        statuses = ['passed', 'failed', 'error', 'skipped', 'expected_failure', 'unexpected_success']
        result = self.run_cases(*(make(status) for status in statuses))
        self.assertEqual([row['status'] for row in result.records], statuses)
        data, xml, _ = self.reports(result)
        self.assertEqual(data['summary'], dict.fromkeys(statuses, 1))
        self.assertEqual({key: xml.attrib[key] for key in ('tests', 'errors', 'failures', 'skipped')},
                         {'tests': '6', 'errors': '1', 'failures': '2', 'skipped': '2'})
        for status in statuses[1:]:
            self.assertFalse(self.run_cases(make(status)).wasSuccessful())

    def test_redaction_precedes_serialization_of_every_field(self):
        secret = 'quote"slash\\line\nsecret'
        class Sample(unittest.TestCase):
            def id(self):
                return secret
            def runTest(self):
                self.evidence = [{secret: [secret]}]
                self.fail(secret)
        with patch.dict(os.environ, {'POOLER_DSN': 'password=ordinary'}):
            result = Result(io.StringIO(), True, 0)
        result.secrets = [secret]
        self.assertEqual(result.redact(secret), '<redacted>')
        self.run_suite(result, Sample())
        data, xml, raw = self.reports(result, {secret: [secret]})
        self.assertNotIn(secret, raw)
        self.assertNotIn(json.dumps(secret)[1:-1], raw)
        self.assertEqual(data['contract'], {'<redacted>': ['<redacted>']})
        self.assertEqual(xml.find('testcase').attrib['name'], '<redacted>')

    def run_suite(self, result, case):
        unittest.TestSuite([case]).run(result)

    def test_cli_exit_propagates_strict_acceptance_and_repeat_inventory(self):
        class Sample(unittest.TestCase):
            def test_outcome(self):
                if outcome == 'skip':
                    self.skipTest('simulated unavailable capability')
                if outcome == 'failure':
                    self.fail('simulated failure')
                if outcome == 'error':
                    raise RuntimeError('simulated error')
            @unittest.expectedFailure
            def expected(self):
                if outcome == 'expected':
                    self.fail('simulated expected failure')
        for outcome in ('pass', 'skip', 'failure', 'error', 'expected', 'unexpected'):
            method = Sample.expected if outcome in ('expected', 'unexpected') else Sample.test_outcome
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                args = ['conformance', '--manifest', 'conformance/manifests/minimal.json',
                        '--json', directory + '/out.json', '--junit', directory + '/out.xml', '--repeat', '2']
                with patch.object(cli, 'CASES', (Sample,)), patch.object(cli, 'endpoints', return_value=('a', 'b')), \
                     patch.object(cli.unittest.TestLoader, 'getTestCaseNames', return_value=[method.__name__]), \
                     patch('sys.argv', args), patch('sys.stderr', io.StringIO()):
                    code = cli.main()
                self.assertEqual(code, int(outcome != 'pass'))
                data = json.loads(Path(directory + '/out.json').read_text())
                self.assertEqual(len(data['results']), 2)
                self.assertEqual(data['accepted'], outcome == 'pass')

    def test_empty_run_is_not_accepted(self):
        self.assertFalse(self.run_cases().wasSuccessful())

    def test_successful_subtests_and_class_teardown(self):
        class Sample(unittest.TestCase):
            def runTest(self):
                with self.subTest(part=1):
                    self.assertTrue(True)
            @classmethod
            def tearDownClass(cls):
                raise RuntimeError('class teardown')
        result = self.run_cases(Sample())
        self.assertEqual([row['status'] for row in result.records], ['passed', 'error'])
        self.assertFalse(result.wasSuccessful())

    def test_control_bytes_keep_xml_parseable_and_json_lossless(self):
        class Sample(unittest.TestCase):
            def runTest(self):
                self.fail('wire-byte: \x00')
        data, _, _ = self.reports(self.run_cases(Sample()))
        self.assertIn('wire-byte: \x00', data['results'][0]['detail'])

    def test_focused_runs_are_explicitly_non_release_evidence(self):
        class Sample(unittest.TestCase):
            def runTest(self):
                pass
        result = self.run_cases(Sample())
        with tempfile.TemporaryDirectory() as directory:
            jp, xp = Path(directory) / 'out.json', Path(directory) / 'out.xml'
            result.write(jp, xp, {}, full_inventory=False)
            data = json.loads(jp.read_text())
        self.assertFalse(data['accepted'])
        self.assertIn('non-release', data['scope'])
