#!/usr/bin/env python3
"""PERF-003/005 independent Memcheck audit: eight modes/client-count arms."""

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TypedDict
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks import compare as c  # noqa: E402
from benchmarks.performance_profile import REAL_POPEN, dump  # noqa: E402
from benchmarks.workload_compare import SCRIPTS, oracle, workload_preflight  # noqa: E402

TARGETS = ('rustybouncer-session', 'rustybouncer-routed',
           'rustybouncer-pool-session', 'rustybouncer-pool-transaction')


class Checked(c.Fixture):
    shared = False

    def connect(self):
        client=super().connect()
        if self.shared:
            client.sock.settimeout(60)
        return client

    def finish(self):
        if self.process:
            if self.process.poll() is None:
                os.killpg(self.process.pid, signal.SIGINT)
                try:
                    self.process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait(timeout=5)
                    raise AssertionError(
                        "owned memory checker shutdown timed out"
                    ) from None
            assert self.process.poll() == 0, (
                f"Memcheck/guest exit status {self.process.poll()}"
            )

    def launch(self, target, directory):
        self.finish()
        self.shared = '-pool-' in target

        def spawn(command, **kwargs):
            # These are instrumentation-only budgets, chosen before running the
            # VM. Native correctness/time-budget suites retain their settings.
            config = Path(command[1])
            text = config.read_text().replace(
                "probe_timeout_ms = 1000", "probe_timeout_ms = 10000"
            )
            text = text.replace(
                "startup_timeout_ms = 5000", "startup_timeout_ms = 30000"
            )
            text = text.replace("wait_timeout_ms = 10000", "wait_timeout_ms = 30000")
            if self.shared:
                text = text.replace('route_selects = false','route_selects = true')
                text = text.replace('wait_timeout_ms = 30000','wait_timeout_ms = 180000')
                mode = 'transaction' if target.endswith('transaction') else 'session'
                text = f'pool_mode = "{mode}"\npool_size = 16\nmax_pool_backends = 16\n' + text
            config.write_text(text)
            argv = [
                "valgrind",
                "--tool=memcheck",
                "--leak-check=full",
                "--show-leak-kinds=all",
                "--errors-for-leak-kinds=definite,indirect,possible",
                "--error-exitcode=97",
                "--default-suppressions=no",
                "--xml=yes",
                "--xml-file=" + str(directory / "memcheck.xml"),
                "--log-file=" + str(directory / "memcheck.log"),
                *command,
            ]
            dump(directory / "checker-command.json", argv)
            return REAL_POPEN(argv, **kwargs, start_new_session=True)

        with patch.object(c.subprocess, "Popen", side_effect=spawn):
            super().launch(target, directory)

    def close(self):
        try:
            self.finish()
        finally:
            super().close()


class Frame(TypedDict):
    function: str
    file: str
    directory: str


class Finding(TypedDict):
    kind: str
    bytes: int
    blocks: int
    frames: list[Frame]


def inspect(path):
    root = ET.parse(path).getroot()
    assert any(s.findtext("state") == "FINISHED" for s in root.findall("status")), (
        "incomplete Memcheck XML"
    )
    findings: list[Finding] = []
    for error in root.findall("error"):
        frames: list[Frame] = [
            {
                "function": f.findtext("fn", ""),
                "file": f.findtext("file", ""),
                "directory": f.findtext("dir", ""),
            }
            for f in error.findall(".//frame")
        ]
        kind = error.findtext("kind", "")
        findings.append(
            {
                "kind": kind,
                "bytes": int(error.findtext("xwhat/leakedbytes", "0")),
                "blocks": int(error.findtext("xwhat/leakedblocks", "0")),
                "frames": frames,
            }
        )
    failures = [r for r in findings if r["kind"] != "Leak_StillReachable"]
    private = [
        r
        for r in findings
        if any(
            "router::wire::" in f["function"]
            or "rustybouncer::buffers::" in f["function"]
            or (f["file"] == "wire.rs" and f["directory"].endswith("/router"))
            or (f["file"] == "buffers.rs" and f["directory"].endswith("/src"))
            for f in r["frames"]
        )
    ]
    assert not failures, f"Memcheck errors: {json.dumps(failures)[:4000]}"
    assert not private, (
        f"retained private response storage: {json.dumps(private)[:4000]}"
    )
    return {
        "definite_indirect_possible_or_access_errors": len(failures),
        "private_response_findings": len(private),
        "other_still_reachable_bytes": sum(r["bytes"] for r in findings),
        "other_still_reachable_blocks": sum(r["blocks"] for r in findings),
        "findings": findings,
    }


def arm(fixture, clients, target, directory):
    directory.mkdir()
    fixture.launch(target, directory)
    if '-pool-' in target:
        workload_preflight(fixture, 'large', clients, directory)
    else:
        c.m.preflight(fixture, clients)
    oracle(fixture, "large")
    command = [
        "pgbench",
        "-h",
        "127.0.0.1",
        "-p",
        str(fixture.proxy_port),
        "-U",
        "postgres",
        "-d",
        "postgres",
        "-n",
        "-M",
        "simple",
        "-c",
        str(clients),
        "-j",
        str(clients),
        "-t",
        "10",
        "-f",
        str(fixture.output / "large.sql"),
    ]
    dump(directory / "load-command.json", command)
    output = subprocess.run(
        command, text=True, capture_output=True, timeout=180, check=False
    )
    (directory / "pgbench.txt").write_text(output.stdout + output.stderr)
    import re

    done = re.search(
        r"number of transactions actually processed:\s*(\d+)", output.stdout
    )
    errors = re.search(r"number of failed transactions:\s*(\d+)", output.stdout)
    assert output.returncode == 0 and done and errors, output.stdout + output.stderr
    assert int(done[1]) == clients * 10 and int(errors[1]) == 0
    oracle(fixture, "large")

    # Independent PG observation after every frontend closes. Background workers
    # and discovery use different identities/types and do not count as clients.
    def quiescent():
        if '-pool-' in target:
            query = ("SELECT count(*),count(*) FILTER (WHERE state<>'idle') FROM pg_stat_activity WHERE "
                     "backend_type='client backend' AND usename='postgres' AND pid<>pg_backend_pid()")
            count, active = map(int, c.m.f.sql(fixture.nodes[0], query).split('|'))
            return count <= 16 and active == 0
        return (
            c.m.f.sql(
                fixture.nodes[0],
                "SELECT count(*) FROM pg_stat_activity WHERE "
                "backend_type='client backend' AND usename='postgres' AND pid<>pg_backend_pid()",
            )
            == "0"
        )

    c.m.f.eventually(
        quiescent, description="all workload backends closed before allocator audit"
    )
    fixture.finish()
    assert c.m.f.sql(fixture.nodes[0], "SELECT count(*) FROM pg_stat_activity WHERE "
                     "backend_type='client backend' AND usename='postgres' AND pid<>pg_backend_pid()") == '0'
    report = inspect(directory / "memcheck.xml")
    result = {
        "clients": clients,
        "target": target,
        "queries": int(done[1]),
        "errors": 0,
        "audit": report,
    }
    dump(directory / "result.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    binary = args.binary.resolve()
    dump(
        output / "environment.json",
        {
            "command": sys.argv,
            "binary": str(binary),
            "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "valgrind": subprocess.check_output(
                ["valgrind", "--version"], text=True
            ).strip(),
            "arms": 8,
            "not_timing_comparable": True,
            "sampling": False,
            "suppressions": False,
            "instrumentation_timeouts_ms": {
                "probe": 10000,
                "startup": 30000,
                "route": 30000,
                "shared_route": 180000,
                "shared_wire_read": 60000,
            },
            "limitation": "VM audit only; no throughput/latency/RSS comparison with native timing",
        },
    )
    (output / "large.sql").write_text(SCRIPTS["large"])
    fixture = Checked(output, binary)
    results = []
    started = time.monotonic()
    try:
        fixture.start()
        for clients in (10, 100):
            for target in TARGETS:
                results.append(
                    arm(fixture, clients, target, output / f"c{clients}-{target}")
                )
                dump(output / "results.json", results)
                print(f"{len(results)}/8 {clients} {target}", flush=True)
    finally:
        fixture.close()
    assert len(results) == 8
    print(f"complete audit in {time.monotonic() - started:.3f}s", flush=True)


if __name__ == "__main__":
    main()
