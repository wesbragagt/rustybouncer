#!/usr/bin/env python3
"""PERF-002/003 full buffered-wire concurrency/limit acceptance (four cases)."""

import concurrent.futures
import contextlib
import hashlib
import json
import os
import struct
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks import compare as c  # noqa: E402
from benchmarks.performance_profile import process_info  # noqa: E402


class Buffering(unittest.TestCase):
    def setUp(self):
        self.output = Path(os.environ["BUFFER_EVIDENCE"]) / self.id().rsplit(".", 1)[-1]
        self.output.mkdir(parents=True, exist_ok=False)
        self.fixture = c.Fixture(
            self.output, Path(os.environ["RUSTYBOUNCER_BIN"]).resolve()
        )
        self.addCleanup(self.fixture.close)
        self.fixture.start()
        directory = self.output / "proxy"
        directory.mkdir()
        self.fixture.launch("rustybouncer-routed", directory)
        self.rows = []
        self.addCleanup(self.save)

    def save(self):
        (self.output / "observations.json").write_text(
            json.dumps(self.rows, indent=2) + "\n"
        )

    def backends(self):
        return int(
            c.m.f.sql(
                self.fixture.nodes[0],
                "SELECT count(*) FROM pg_stat_activity WHERE backend_type='client backend' AND usename='postgres' AND pid<>pg_backend_pid()",
            )
        )

    def concurrent(self, count):
        barrier = threading.Barrier(count + 1, timeout=30)
        release = threading.Event()

        def worker(index):
            with contextlib.closing(self.fixture.connect()) as client:
                barrier.wait()
                self.assertTrue(
                    release.wait(timeout=30), "concurrency release deadline"
                )
                for cycle in range(5):
                    first = 1 + (index * 37 + cycle * 13) % 8000
                    last = first + 1023
                    began = time.monotonic()
                    rows = client.query(
                        f"SELECT id,payload,NULL::text FROM comparison WHERE id BETWEEN {first} AND {last} ORDER BY id"
                    )
                    expected = [
                        [
                            str(key),
                            hashlib.md5(
                                str(key).encode(), usedforsecurity=False
                            ).hexdigest()
                            * 4,
                            None,
                        ]
                        for key in range(first, last + 1)
                    ]
                    self.assertEqual(
                        rows,
                        expected,
                        f"wrong client/cycle results: client={index} cycle={cycle}",
                    )
                    self.assertEqual(
                        client.query(f"SELECT {index}, {cycle}"),
                        [[str(index), str(cycle)]],
                    )
                    observation = {
                        "client": index,
                        "cycle": cycle,
                        "seconds": time.monotonic() - began,
                        "row_count": len(rows),
                    }
                    with lock:
                        self.rows.append(observation)

        lock = threading.Lock()
        with concurrent.futures.ThreadPoolExecutor(max_workers=count) as executor:
            pending = [executor.submit(worker, index) for index in range(count)]
            try:
                barrier.wait()
                c.m.f.eventually(
                    lambda: self.backends() == count,
                    description="all provisioned backend owners present",
                )
                self.rows.append(
                    {"held_backend_connections": self.backends(), "clients": count}
                )
            finally:
                release.set()
            for future in pending:
                future.result(timeout=60)
        c.m.f.eventually(
            lambda: self.backends() == 0, description="all client backend leases closed"
        )
        assert self.fixture.process
        self.rows.append(
            {
                "after_disconnect": process_info(self.fixture.process.pid),
                "backend_connections": self.backends(),
            }
        )
        self.assertEqual(sum("row_count" in row for row in self.rows), count * 5)

    def test_10_clients_multirow_null_and_unique_results(self):
        self.concurrent(10)

    def test_100_clients_multirow_null_and_unique_results(self):
        self.concurrent(100)

    def test_pipelined_query_error_keeps_later_responses_ordered(self):
        with contextlib.closing(self.fixture.connect()) as client:
            queries = [
                "SELECT 71",
                "SELECT 1/0",
                "SELECT 93",
                "SELECT NULL::text",
                "SELECT 105",
            ]
            for query in queries:
                body = query.encode() + b"\0"
                client.sock.sendall(b"Q" + struct.pack("!I", len(body) + 4) + body)
            self.assertEqual(client.read_cycle(), [["71"]])
            with self.assertRaises(c.m.f.PgError) as error:
                client.read_cycle()
            self.assertEqual(error.exception.code, "22012")
            self.assertEqual(client.read_cycle(), [["93"]])
            self.assertEqual(client.read_cycle(), [[None]])
            self.assertEqual(client.read_cycle(), [["105"]])
            self.assertEqual(client.query("SELECT 117"), [["117"]])

    def test_message_and_result_overflow_discard_before_exposure(self):
        for query in (
            "SELECT repeat('x',16777216)",
            "SELECT repeat('x',1048576) FROM generate_series(1,33)",
        ):
            with contextlib.closing(self.fixture.connect()) as client:
                client.send_query(query)
                # A single byte would expose an unsuccessful private attempt.
                try:
                    first = client.sock.recv(1)
                except ConnectionResetError:
                    first = b""
                self.assertEqual(first, b"", "overflow exposed buffered result data")
            c.m.f.eventually(
                lambda: self.backends() == 0, description="overflow backend discarded"
            )
            with contextlib.closing(self.fixture.connect()) as replacement:
                self.assertEqual(replacement.query("SELECT 129"), [["129"]])
        c.m.f.eventually(
            lambda: self.backends() == 0, description="post-overflow cleanup"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
