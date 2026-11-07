#!/usr/bin/env python3
"""Owned Docker integration resources; run the entire file, without exclusions."""
import concurrent.futures
import socket
import time
import unittest
from pathlib import Path

import failover as fixtures


class LongQueries(unittest.TestCase):
    def setUp(self):
        self.cluster = fixtures.Cluster()
        self.addCleanup(self.cluster.close)
        self.cluster.start()
        fixtures.sql(self.cluster.nodes[0], """
            CREATE VIEW slow_read AS
            SELECT current_setting('cluster_name') AS node, 42 AS value
            FROM pg_sleep(2);
            CREATE VIEW partial_read AS
            SELECT n, current_setting('cluster_name') AS node, repeat('x', 1000) AS payload
            FROM generate_series(1, 300) AS n, LATERAL pg_sleep(n * 0.00005);
        """)
        for node in self.cluster.nodes[1:]:
            fixtures.eventually(lambda node=node: fixtures.sql(
                node, "SELECT count(*) FROM pg_class WHERE relname='partial_read'") == "1")
        self.configure()

    def configure(self, attempts=3, total=8000, execution=6000):
        process = self.cluster.process
        assert process is not None
        process.terminate()
        process.wait(timeout=5)
        config = Path(self.cluster.directory.name) / "config.toml"
        text = config.read_text().replace("wait_timeout_ms = 4000", "wait_timeout_ms = 600")
        text = "\n".join(line for line in text.splitlines() if not line.startswith(
            ("query_timeout_ms", "read_retry_timeout_ms", "read_max_attempts")))
        text = text.replace('probe_user =', f'query_timeout_ms = {execution}\n'
                            f'read_retry_timeout_ms = {total}\n'
                            f'read_max_attempts = {attempts}\nprobe_user =')
        config.write_text(text)
        self.cluster.process = fixtures.subprocess.Popen(
            [str(fixtures.BINARY), str(config)], stdout=self.cluster.log, stderr=self.cluster.log)
        fixtures.eventually(self.cluster.ready)
        fixtures.eventually(self.cluster.replicas_discovered)

    def client(self):
        client = self.cluster.connect()
        self.addCleanup(client.close)
        return client

    def active_node(self, query):
        def find():
            for index, node in enumerate(self.cluster.nodes):
                if index in self.cluster.paused:
                    continue
                count = fixtures.sql(node, "SELECT count(*) FROM pg_stat_activity WHERE "
                                     f"query='{query}' AND state='active'")
                if count == "1":
                    return index + 1
            return None
        return fixtures.eventually(find) - 1

    def test_healthy_execution_exceeds_routing_wait(self):
        started = time.monotonic()
        self.assertEqual(self.client().query("SELECT value FROM slow_read"), [["42"]])
        self.assertGreater(time.monotonic() - started, 1.8)

    def test_unrelated_primary_loss_and_promotion(self):
        client = self.client()
        query = "SELECT node, value FROM slow_read"
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(client.query, query)
            active = self.active_node(query)
            self.cluster.pause(0)
            self.cluster.promote(3 - active)
            self.assertEqual(future.result(timeout=8), [["bc"[active - 1], "42"]])

    def test_promoted_backend_drains(self):
        client = self.client()
        query = "SELECT node, value FROM slow_read"
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(client.query, query)
            active = self.active_node(query)
            self.cluster.pause(0)
            self.cluster.promote(active)
            self.assertEqual(future.result(timeout=8), [["bc"[active - 1], "42"]])

    def test_failed_read_retries_without_partial_results(self):
        client = self.client()
        query = "SELECT n, node, payload FROM partial_read"
        client.send_query(query)
        active = self.active_node(query)
        # Some rows are produced before completion, but none may reach the client.
        client.sock.settimeout(0.15)
        with self.assertRaises(socket.timeout):
            client.sock.recv(1, socket.MSG_PEEK)
        fixtures.sql(self.cluster.nodes[active], "SELECT pg_terminate_backend(pid) FROM "
                     f"pg_stat_activity WHERE query='{query}' AND state='active'")
        client.sock.settimeout(10)
        rows = client.read_cycle()
        self.assertEqual([row[0] for row in rows], [str(i) for i in range(1, 301)])
        self.assertEqual({row[1] for row in rows}, {"bc"[2 - active]})
        self.assertEqual({row[2] for row in rows}, {"x" * 1000})

    def test_attempt_limit(self):
        self.configure(attempts=1)
        client = self.client()
        query = "SELECT value FROM slow_read"
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(client.query, query)
            active = self.active_node(query)
            self.cluster.pause(active)
            with self.assertRaises((fixtures.PgError, OSError)):
                future.result(timeout=4)

    def test_total_deadline(self):
        self.configure(total=900)
        started = time.monotonic()
        with self.assertRaises((fixtures.PgError, OSError)):
            self.client().query("SELECT value FROM slow_read")
        self.assertLess(time.monotonic() - started, 2.5)

    def test_dispatched_write_not_replayed(self):
        client = self.client()
        query = "INSERT INTO continuity SELECT 91, 'write' FROM pg_sleep(3)"
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(client.query, query)
            self.assertEqual(self.active_node(query.replace("'", "''")), 0)
            self.cluster.pause(0)
            self.cluster.promote()
            with self.assertRaises((fixtures.PgError, OSError)):
                future.result(timeout=5)
        self.assertEqual(fixtures.sql(self.cluster.nodes[1],
                                     "SELECT count(*) FROM continuity WHERE id=91"), "0")

    def test_interrupted_transaction_not_replayed(self):
        client = self.client()
        client.query("BEGIN")
        client.query("INSERT INTO continuity VALUES (92, 'transaction')")
        self.cluster.pause(0)
        self.cluster.promote()
        with self.assertRaises((fixtures.PgError, OSError)):
            client.query("COMMIT")
        self.assertEqual(fixtures.sql(self.cluster.nodes[1],
                                     "SELECT count(*) FROM continuity WHERE id=92"), "0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
