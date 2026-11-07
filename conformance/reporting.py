"""Machine-readable results with redacted diagnostics and per-case evidence."""
import json
import os
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
        for secret in self.secrets:
            message = message.replace(secret, "<redacted>")
        return message

    def _exc_info_to_string(self, err, test):
        return self.redact("".join(traceback.format_exception(*err)))

    def startTest(self, test):
        self.started[id(test)] = time.monotonic()
        super().startTest(test)

    def record(self, test, status, detail=""):
        self.records.append({"test": test.id(), "status": status, "detail": self.redact(detail),
                             "duration_s": time.monotonic() - self.started[id(test)],
                             "evidence": getattr(test, "evidence", [])})

    def addSuccess(self, test):
        self.record(test, "passed")
        super().addSuccess(test)

    def addSkip(self, test, reason):
        self.record(test, "skipped", reason)
        super().addSkip(test, reason)

    def addFailure(self, test, err):
        self.record(test, "failed", self._exc_info_to_string(err, test))
        super().addFailure(test, err)

    def addError(self, test, err):
        self.record(test, "error", self._exc_info_to_string(err, test))
        super().addError(test, err)

    def write(self, json_path: Path, junit_path: Path, contract: dict) -> None:
        data = {"contract": contract, "results": self.records,
                "summary": {status: sum(row["status"] == status for row in self.records)
                            for status in ("passed", "skipped", "failed", "error")},
                "acceptance": "declared-contract results only; see release acceptance checklist"}
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(self.redact(json.dumps(data, indent=2)))
        root = ET.Element("testsuite", name="pooler-conformance", tests=str(len(self.records)),
                          failures=str(len(self.failures)), errors=str(len(self.errors)), skipped=str(len(self.skipped)))
        for row in self.records:
            case = ET.SubElement(root, "testcase", name=row["test"], time=str(row["duration_s"]))
            tag = {"failed": "failure", "error": "error", "skipped": "skipped"}.get(row["status"])
            if tag:
                ET.SubElement(case, tag).text = row["detail"]
            ET.SubElement(case, "system-out").text = self.redact(json.dumps(row["evidence"]))
        junit_path.parent.mkdir(parents=True, exist_ok=True)
        ET.ElementTree(root).write(junit_path, encoding="unicode", xml_declaration=True)
