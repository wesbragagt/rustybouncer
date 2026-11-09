"""Machine-readable results with redacted diagnostics and per-case evidence."""
import json
import os
import re
import time
import traceback
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict


class Result(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []
        self.started = {}
        self._active = {}
        self.secrets: list[str] = []
        for key, value in os.environ.items():
            if key.endswith("_DSN"):
                try:
                    password = conninfo_to_dict(value).get("password")
                    if password:
                        self.secrets.append(str(password))
                except Exception:
                    pass

    def redact(self, message: str) -> str:
        for secret in sorted(self.secrets, key=len, reverse=True):
            message = message.replace(secret, "<redacted>")
        return message

    def _exc_info_to_string(self, err, test):
        return self.redact("".join(traceback.format_exception(*err)))

    def startTest(self, test):
        self.started[id(test)] = time.monotonic()
        self._active[id(test)] = {"test": test.id(), "status": "passed", "detail": "",
                                  "duration_s": 0.0, "evidence": []}
        super().startTest(test)

    def stopTest(self, test):
        row = self._active.pop(id(test))
        row["duration_s"] = time.monotonic() - self.started.pop(id(test))
        row["evidence"] = self._redact_value(getattr(test, "evidence", []))
        self.records.append(self._redact_value(row))
        super().stopTest(test)

    def record(self, test, status, detail=""):
        row = self._active.get(id(test))
        if row is None:
            # unittest reports class/module fixture failures without startTest.
            self.records.append(self._redact_value({
                "test": test.id(), "status": status, "detail": detail,
                "duration_s": 0.0, "evidence": getattr(test, "evidence", [])}))
            return
        priority = {"passed": 0, "skipped": 1, "expected_failure": 2,
                    "unexpected_success": 3, "failed": 4, "error": 5}
        if priority[status] > priority[row["status"]]:
            row["status"] = status
        if detail:
            row["detail"] += ("\n" if row["detail"] else "") + self.redact(detail)

    def _redact_value(self, value):
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {self.redact(str(key)): self._redact_value(item)
                    for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._redact_value(item) for item in value]
        return value

    def addSuccess(self, test):
        super().addSuccess(test)

    def addSkip(self, test, reason):
        parent = getattr(test, "test_case", test)
        self.record(parent, "skipped", f"{test.id()}: {reason}")
        super().addSkip(test, self.redact(reason))

    def addFailure(self, test, err):
        self.record(test, "failed", self._exc_info_to_string(err, test))
        super().addFailure(test, err)

    def addError(self, test, err):
        self.record(test, "error", self._exc_info_to_string(err, test))
        super().addError(test, err)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            status = "failed" if issubclass(err[0], test.failureException) else "error"
            self.record(test, status, f"{subtest.id()}:\n{self._exc_info_to_string(err, test)}")
        super().addSubTest(test, subtest, err)

    def addExpectedFailure(self, test, err):
        self.record(test, "expected_failure", self._exc_info_to_string(err, test))
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test):
        self.record(test, "unexpected_success", "Expected failure unexpectedly succeeded")
        super().addUnexpectedSuccess(test)

    def wasSuccessful(self):
        return (self.testsRun > 0 and super().wasSuccessful() and not self.skipped and not self.expectedFailures
                and not self._active and all(row["status"] == "passed" for row in self.records))

    def write(self, json_path: Path, junit_path: Path, contract: dict, *, full_inventory: bool = True) -> None:
        statuses = ("passed", "skipped", "failed", "error", "expected_failure", "unexpected_success")
        records = self._redact_value(self.records)
        summary = {status: sum(row["status"] == status for row in records) for status in statuses}
        data = {"contract": self._redact_value(contract), "results": records, "summary": summary,
                "diagnostic_counts": {"failed": len(self.failures), "error": len(self.errors),
                                      "skipped": len(self.skipped),
                                      "expected_failure": len(self.expectedFailures),
                                      "unexpected_success": len(self.unexpectedSuccesses)},
                "accepted": self.wasSuccessful() and full_inventory,
                "scope": "full inventory" if full_inventory else "focused developer run; non-release evidence",
                "acceptance": "declared-contract results only; see release acceptance checklist"}
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(data, indent=2))
        root = ET.Element("testsuite", name="pooler-conformance", tests=str(len(records)),
                          failures=str(summary["failed"] + summary["unexpected_success"]),
                          errors=str(summary["error"]),
                          skipped=str(summary["skipped"] + summary["expected_failure"]),
                          time=str(sum(row["duration_s"] for row in records)))
        tags = {"failed": "failure", "error": "error", "skipped": "skipped",
                "expected_failure": "skipped", "unexpected_success": "failure"}
        for row in records:
            case = ET.SubElement(root, "testcase", name=row["test"], time=str(row["duration_s"]))
            tag = tags.get(row["status"])
            if tag:
                ET.SubElement(case, tag, type=row["status"]).text = row["detail"]
            ET.SubElement(case, "system-out").text = json.dumps(row["evidence"])
        junit_path.parent.mkdir(parents=True, exist_ok=True)
        xml = ET.tostring(root, encoding="unicode", xml_declaration=True)
        # Diagnostics can contain wire control bytes, which XML 1.0 forbids.
        xml = re.sub(r"[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]", "�", xml)
        junit_path.write_text(xml)
