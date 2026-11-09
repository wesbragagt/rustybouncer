"""Offline checks of contract validation, cancellation bounds and report semantics."""
import asyncio
import io
import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from conformance.base import Case
from conformance.client import bounded
from conformance.config import Contract, endpoints
from conformance.reporting import Result

ROOT = Path(__file__).resolve().parents[1]


class Contracts(unittest.TestCase):
    def test_sample_manifests_validate(self):
        for manifest in (ROOT / "conformance/manifests").glob("*.json"):
            self.assertTrue(Contract.load(manifest).name)

    def invalid(self, change):
        data = json.loads((ROOT / "conformance/manifests/minimal.json").read_text())
        change(data)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contract.json"
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                Contract.load(path)

    def test_unknown_feature_rejected(self):
        self.invalid(lambda data: data["features"].update(magic=True))

    def test_false_string_is_not_boolean(self):
        self.invalid(lambda data: data["features"].update(cancel="false"))

    def test_unbounded_timeout_rejected(self):
        self.invalid(lambda data: data["settings"].update(operation_timeout=float("inf")))

    def test_missing_limit_configuration_rejected(self):
        def change(data):
            data["features"]["limits"] = True
            del data["settings"]["pool_size"]
        self.invalid(change)

    def test_pooling_mode_cannot_disable_required_semantics(self):
        self.invalid(lambda data: data.update(mode="transaction"))

    def test_no_implicit_localhost_endpoint(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            endpoints()

    def test_shell_adapter_strings_rejected(self):
        self.invalid(lambda data: data.update(actions={"reload": "sh -c something"}))

    def test_paired_fault_actions_require_restore_before_execution(self):
        case = Case()
        case.contract = Contract("fixture", "routing", {}, actions={"network_cut": ["false"]})
        with patch.dict(os.environ, {"CONFORMANCE_ALLOW_FAULTS": "1"}), self.assertRaises(unittest.SkipTest):
            case.require_actions("network_cut", "network_restore")

    def test_reports_preserve_skips_and_redact_credentials(self):
        class Sample(unittest.TestCase):
            def test_skip(self):
                self.skipTest("optional TLS unavailable")
        with patch.dict(os.environ, {"POOLER_DSN": "password=example-test-password"}):
            result = Result(io.StringIO(), True, 1)
            unittest.defaultTestLoader.loadTestsFromTestCase(Sample).run(result)
            self.assertEqual(result.redact("example-test-password"), "<redacted>")
            with tempfile.TemporaryDirectory() as directory:
                json_path, xml_path = Path(directory) / "results.json", Path(directory) / "results.xml"
                result.write(json_path, xml_path, {})
                self.assertEqual(json.loads(json_path.read_text())["summary"]["skipped"], 1)
                self.assertIsNotNone(ET.parse(xml_path).find("testcase/skipped"))


class Deadlines(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_disposes_and_stops_operation(self):
        aborted = []
        event = asyncio.Event()
        with self.assertRaises(TimeoutError):
            await bounded(event.wait(), .01, lambda: aborted.append(True))
        self.assertEqual(aborted, [True])

    async def test_caller_cancellation_stops_inner_task(self):
        aborted = []
        event = asyncio.Event()
        task = asyncio.create_task(bounded(event.wait(), 10, lambda: aborted.append(True)))
        await asyncio.sleep(0)  # Schedule the operation, not a database timing assumption.
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(aborted, [True])
