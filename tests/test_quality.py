"""Check the actual analyzer's boundary and failure exit status in both languages."""

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ComplexityGate(unittest.TestCase):
    def run_fixture(self, suffix: str, branches: int) -> subprocess.CompletedProcess[str]:
        if suffix == ".rs":
            code = "pub fn threshold(x: i32) -> i32 { let mut y = 0;\n"
            code += "if x > 0 { y += 1; }\n" * branches
            code += "y\n}\n"
        else:
            code = "def threshold(x):\n    y = 0\n"
            code += "    if x > 0:\n        y += 1\n" * branches
            code += "    return y\n"
        return self.run_code(suffix, code)

    def run_code(self, suffix: str, code: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / f"threshold{suffix}"
            fixture.write_text(code)
            return subprocess.run(
                [sys.executable, str(ROOT / "scripts/check_complexity.py"), "--max-complexity", "30", str(fixture)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

    def test_complexity_30_passes(self):
        for suffix in (".rs", ".py"):
            with self.subTest(language=suffix):
                result = self.run_fixture(suffix, 29)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("maximum 30", result.stdout)

    def test_complexity_31_fails(self):
        for suffix in (".rs", ".py"):
            with self.subTest(language=suffix):
                result = self.run_fixture(suffix, 30)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("complexity 31 exceeds 30", result.stderr)


    def test_nested_python_function_is_checked_independently(self):
        inner = "def threshold(x):\n    y = 0\n" + "    if x > 0:\n        y += 1\n" * 30 + "    return y\n"
        code = "def outer():\n" + textwrap.indent(inner, "    ") + "    return threshold\n"
        result = self.run_code(".py", code)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(": threshold: cyclomatic complexity 31", result.stderr)
        self.assertNotIn(": outer:", result.stderr)

    def test_rust_closure_is_checked_independently(self):
        code = "pub fn outer(x: i32) -> i32 { let run = || { let mut y = 0;\n"
        code += "if x > 0 { y += 1; }\n" * 30
        code += "y }; run() }\n"
        result = self.run_code(".rs", code)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(": <anonymous>: cyclomatic complexity 31", result.stderr)
        self.assertNotIn(": outer:", result.stderr)
