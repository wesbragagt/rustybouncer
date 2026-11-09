#!/usr/bin/env python3
"""PERF-006/Q05/Q06: real cancellation, aggregate allocation and lifecycle tests."""

import concurrent.futures
import contextlib
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import failover as f  # noqa: E402
from metrics_support import MetricsCluster  # noqa: E402


class Resources(unittest.TestCase):
    def setUp(self):
        self.evidence = Path(os.environ["RESOURCE_EVIDENCE"]) / self._testMethodName
        self.cluster = MetricsCluster(self.evidence)
        self.addCleanup(self.cluster.close)
        self.cluster.start()
        f.sql(
            self.cluster.nodes[0],
            "CREATE VIEW cancel_slow AS SELECT 71 AS value FROM pg_sleep(30); "
            "CREATE VIEW resource_large AS SELECT repeat('x',1048576) AS payload FROM generate_series(1,8)",
        )
        for node in self.cluster.nodes[1:]:
            f.eventually(
                lambda node=node: (
                    f.sql(
                        node,
                        "SELECT count(*) FROM pg_class WHERE relname IN ('cancel_slow','resource_large')",
                    )
                    == "2"
                )
            )

    def client(self):
        client = self.cluster.connect()
        self.addCleanup(client.close)
        return client

    def cancel(self, client, key=None):
        pid, secret = key or client.cancel_key
        with socket.create_connection(
            ("127.0.0.1", self.cluster.proxy_port), timeout=5
        ) as sock:
            sock.sendall(struct.pack("!IIII", 16, 80877102, pid, secret))
            self.assertEqual(sock.recv(1), b"")

    def active(self, query):
        escaped = query.replace("'", "''")

        def find():
            for index, node in enumerate(self.cluster.nodes):
                if (
                    index not in self.cluster.paused
                    and f.sql(
                        node,
                        f"SELECT count(*) FROM pg_stat_activity WHERE query='{escaped}' AND state='active'",
                    )
                    == "1"
                ):
                    return index + 1
            return None

        return f.eventually(find, description="query independently observed active") - 1

    def idle(self):
        snapshot = self.cluster.idle()
        self.assertEqual(snapshot.value("result_buffer_reserved_bytes"), 0)
        return snapshot

    def assert_cancelled(self, client):
        with self.assertRaises(f.PgError) as error:
            client.read_cycle()
        self.assertEqual(error.exception.code, "57014")

    def test_replica_cancel_stops_retry_and_preserves_connection(self):
        client = self.client()
        query = "SELECT value FROM cancel_slow"
        before = self.cluster.scrape()
        client.send_query(query)
        self.assertIn(self.active(query), (1, 2))
        self.cancel(client)
        self.assert_cancelled(client)
        self.assertEqual(client.query("SELECT 101"), [["101"]])
        after = self.cluster.scrape()
        self.assertEqual(
            after.value("read_retries_total"), before.value("read_retries_total")
        )
        client.close()
        self.idle()

    def test_dispatched_primary_write_cancellation_is_not_replayed(self):
        client = self.client()
        query = "INSERT INTO continuity SELECT 701,'cancel' FROM pg_sleep(30)"
        client.send_query(query)
        self.assertEqual(self.active(query), 0)
        self.cancel(client)
        self.assert_cancelled(client)
        self.assertEqual(
            f.sql(
                self.cluster.nodes[0], "SELECT count(*) FROM continuity WHERE id=701"
            ),
            "0",
        )
        self.assertEqual(client.query("SELECT 102"), [["102"]])
        client.close()
        self.idle()

    def test_cancel_preserves_savepoint_and_prior_transaction_write(self):
        client = self.client()
        client.query("BEGIN")
        client.query("INSERT INTO continuity VALUES (702,'retained')")
        client.query("SAVEPOINT retained")
        query = "SELECT value FROM cancel_slow"
        client.send_query(query)
        self.assertEqual(self.active(query), 0)
        self.cancel(client)
        self.assert_cancelled(client)
        self.assertEqual(client.transaction_status, b"E")
        client.query("ROLLBACK TO SAVEPOINT retained")
        self.assertEqual(client.transaction_status, b"T")
        client.query("COMMIT")
        self.assertEqual(
            f.sql(self.cluster.nodes[0], "SELECT payload FROM continuity WHERE id=702"),
            "retained",
        )
        client.close()
        self.idle()

    def test_cancel_during_backend_handshake_releases_connect_ownership(self):
        self.cluster.stop_proxy()
        path = Path(self.cluster.directory.name) / "config.toml"
        text = (
            path.read_text()
            .replace("probe_interval_ms = 100", "probe_interval_ms = 10000")
            .replace("probe_timeout_ms = 300", "probe_timeout_ms = 3000")
        )
        path.write_text(text)
        (self.evidence / "handshake-config.toml").write_text(text)
        self.cluster.process = subprocess.Popen(
            [str(f.BINARY), str(path)], stdout=self.cluster.log, stderr=self.cluster.log
        )
        f.eventually(self.cluster.ready)
        f.eventually(self.cluster.replicas_discovered)
        client = self.client()
        before = self.cluster.scrape()
        for index in (1, 2):
            self.cluster.pause(index)
        client.send_query("SELECT 109")
        f.eventually(
            lambda: (
                sum(
                    self.cluster.scrape().value("backend_connections_active", node=name)
                    for name in self.cluster.names[1:]
                )
                == 1
            ),
            description="TCP connected while PG startup is held",
        )
        self.cancel(client)
        self.assert_cancelled(client)
        after = self.cluster.scrape()
        self.assertEqual(
            after.total("query_attempts_total"), before.total("query_attempts_total")
        )
        self.assertEqual(
            after.value("read_retries_total"), before.value("read_retries_total")
        )
        for index in (1, 2):
            self.cluster.unpause(index)
        self.assertEqual(client.query("SELECT 110"), [["110"]])
        client.close()
        self.idle()

    def test_cancel_during_no_route_wait_does_not_dispatch_or_retry(self):
        client = self.client()
        for index in range(3):
            self.cluster.pause(index)
        f.eventually(
            lambda: (
                self.cluster.scrape().value("topology_nodes", role="unavailable") == 3
            )
        )
        before = self.cluster.scrape()
        client.send_query("SELECT 103")
        f.eventually(lambda: self.cluster.scrape().value("queries_in_flight") == 1)
        self.cancel(client)
        self.assert_cancelled(client)
        after = self.cluster.scrape()
        self.assertEqual(
            after.total("query_attempts_total"), before.total("query_attempts_total")
        )
        self.assertEqual(
            after.value("read_retries_total"), before.value("read_retries_total")
        )
        for index in range(3):
            self.cluster.unpause(index)
        f.eventually(self.cluster.replicas_discovered)
        self.assertEqual(client.query("SELECT 104"), [["104"]])
        client.close()
        self.idle()

    def test_wrong_and_stale_frontend_secrets_do_not_cancel_another_client(self):
        former = self.client()
        stale = former.cancel_key
        former.close()
        self.idle()
        client = self.client()
        query = "SELECT value FROM cancel_slow"
        client.send_query(query)
        node = self.active(query)
        pid, secret = client.cancel_key
        self.cancel(client, (pid, secret ^ 1))
        self.cancel(client, stale)
        self.assertEqual(self.active(query), node)
        self.cancel(client)
        self.assert_cancelled(client)
        self.assertEqual(client.query("SELECT 105"), [["105"]])
        client.close()
        self.idle()

    def test_disconnect_cancels_active_read_and_refunds_every_charge(self):
        client = self.client()
        before = self.cluster.scrape()
        query = "SELECT value FROM cancel_slow"
        client.send_query(query)
        self.active(query)
        client.close()
        after = self.idle()
        self.assertEqual(
            after.value("read_retries_total"), before.value("read_retries_total")
        )
        for node in self.cluster.nodes:
            self.assertEqual(
                f.sql(
                    node,
                    "SELECT count(*) FROM pg_stat_activity WHERE query='SELECT value FROM cancel_slow' AND state='active'",
                ),
                "0",
            )

    def test_pipelined_disconnect_does_not_hide_eof_or_leave_work_running(self):
        client = self.client()
        query = "SELECT value FROM cancel_slow"
        before = self.cluster.scrape()
        client.send_query(query)
        self.active(query)
        for _ in range(4):
            client.send_query("SELECT 106")
        client.close()
        after = self.idle()
        self.assertEqual(
            after.total("queries_total") - before.total("queries_total"), 5
        )
        self.assertEqual(
            after.total("queries_total", outcome="aborted"),
            before.total("queries_total", outcome="aborted"),
        )

    def test_cancel_ready_races_cannot_cancel_the_next_query(self):
        client = self.client()
        outcomes = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            for index in range(40):
                client.send_query(f"SELECT {index}")
                delivery = executor.submit(self.cancel, client)
                try:
                    self.assertEqual(client.read_cycle(), [[str(index)]])
                    outcomes.append("completed")
                except f.PgError as error:
                    self.assertEqual(error.code, "57014")
                    outcomes.append("cancelled")
                delivery.result(timeout=5)
                self.assertEqual(
                    client.query(f"SELECT {index + 1000}"), [[str(index + 1000)]]
                )
        (self.evidence / "race-outcomes.json").write_text(json.dumps(outcomes))
        client.close()
        self.idle()

    def test_cancel_admission_remains_available_at_sql_client_capacity(self):
        self.cluster.restart(capacity=2)
        first, second = self.client(), self.client()
        first.send_query("SELECT value FROM cancel_slow")
        self.active("SELECT value FROM cancel_slow")
        self.assertEqual(second.query("SELECT 107"), [["107"]])
        self.assertEqual(self.cluster.scrape().value("clients_active"), 2)
        self.cancel(first)
        self.assert_cancelled(first)
        self.assertEqual(first.query("SELECT 108"), [["108"]])
        first.close()
        second.close()
        self.idle()

    def test_shutdown_during_connection_churn_reaps_all_backend_owners(self):
        clients = [self.client() for _ in range(100)]
        barrier = threading.Barrier(101, timeout=20)

        def close(client):
            barrier.wait()
            client.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=100) as executor:
            futures = [executor.submit(close, client) for client in clients]
            barrier.wait()
            assert self.cluster.process
            os.kill(self.cluster.process.pid, signal.SIGINT)
            self.assertEqual(self.cluster.process.wait(timeout=5), 0)
            for future in futures:
                future.result(timeout=5)
        for node in self.cluster.nodes:
            f.eventually(
                lambda node=node: (
                    f.sql(
                        node,
                        "SELECT count(*) FROM pg_stat_activity WHERE "
                        "backend_type='client backend' AND usename='postgres' AND pid<>pg_backend_pid()",
                    )
                    == "0"
                ),
                description="all owned PostgreSQL connections closed after one shutdown signal",
            )

    def configured_budget(self):
        self.cluster.stop_proxy()
        path = Path(self.cluster.directory.name) / "config.toml"
        text = path.read_text().replace(
            "probe_user =",
            "result_buffer_bytes = 33554432\ndelivery_timeout_ms = 2000\nprobe_user =",
        )
        path.write_text(text)
        (self.evidence / "budget-config.toml").write_text(text)
        self.cluster.process = subprocess.Popen(
            [str(f.BINARY), str(path)], stdout=self.cluster.log, stderr=self.cluster.log
        )
        f.eventually(self.cluster.ready)
        f.eventually(self.cluster.replicas_discovered)

    def hold_large(self, client):
        before = self.cluster.scrape().total("query_attempts_total", outcome="ok")
        client.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        client.send_query("SELECT payload FROM resource_large")

        def complete():
            snapshot = self.cluster.scrape()
            return (
                snapshot
                if snapshot.total("query_attempts_total", outcome="ok") > before
                and snapshot.value("result_buffer_reserved_bytes") > 0
                else None
            )

        return f.eventually(
            complete, description="complete large response held by a slow consumer"
        )

    def mixed(self, count):
        from benchmarks.performance_profile import process_info

        self.configured_budget()
        snapshots = []
        for cycle in range(3):
            clients = [self.client() for _ in range(count)]
            slow, small = clients[: count // 2], clients[count // 2 :]
            try:
                before = self.cluster.scrape()
                self.hold_large(slow[0])
                for client in slow[1:]:
                    client.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
                    client.send_query("SELECT payload FROM resource_large")
                # Slow consumers keep successful large responses charged. Other
                # large responses are explicitly rejected before exposure.
                held = f.eventually(
                    lambda: (
                        self.cluster.scrape()
                        if self.cluster.scrape().value("result_buffer_rejections_total")
                        > before.value("result_buffer_rejections_total")
                        else None
                    )
                )
                self.assertGreater(held.value("result_buffer_reserved_bytes"), 0)
                barrier = threading.Barrier(len(small), timeout=20)
                latencies = []
                latency_lock = threading.Lock()
                began = time.monotonic()

                def worker(item):
                    index, client = item
                    barrier.wait()
                    for repeat in range(5):
                        started = time.monotonic()
                        self.assertEqual(
                            client.query(f"SELECT {index},{repeat}"),
                            [[str(index), str(repeat)]],
                        )
                        with latency_lock:
                            latencies.append((time.monotonic() - started) * 1000)

                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=len(small)
                ) as executor:
                    for future in [
                        executor.submit(worker, item) for item in enumerate(small)
                    ]:
                        future.result(timeout=20)
                snapshot = self.cluster.scrape()
                self.assertLessEqual(
                    snapshot.value("result_buffer_highwater_bytes"), 33554432
                )
                self.assertEqual(snapshot.value("result_buffer_limit_bytes"), 33554432)
                assert self.cluster.process
                snapshots.append(
                    {
                        "cycle": cycle,
                        "clients": count,
                        "resources": process_info(self.cluster.process.pid),
                        "charged_bytes": snapshot.value("result_buffer_reserved_bytes"),
                        "allocated_bytes": snapshot.value(
                            "result_buffer_allocated_bytes"
                        ),
                        "small_query_latencies_ms": latencies,
                        "small_query_seconds": time.monotonic() - began,
                        "highwater_bytes": snapshot.value(
                            "result_buffer_highwater_bytes"
                        ),
                    }
                )
            finally:
                for client in clients:
                    client.close()
            after = self.idle()
            self.assertEqual(after.value("result_buffer_allocated_bytes"), 33554432)
            snapshots[-1]["after_disconnect_resources"] = process_info(
                self.cluster.process.pid
            )
            snapshots[-1]["after_disconnect_allocated_bytes"] = after.value(
                "result_buffer_allocated_bytes"
            )
        # Full ordered large-result correctness after all saturation cycles.
        with contextlib.closing(self.cluster.connect()) as client:
            self.assertEqual(
                client.query("SELECT payload FROM resource_large"),
                [["x" * 1048576]] * 8,
            )
        self.idle()
        (self.evidence / "memory-cycles.json").write_text(
            json.dumps(snapshots, indent=2)
        )

    def test_slow_receiver_delivery_deadline_refunds_capacity(self):
        self.configured_budget()
        client = self.client()
        started = time.monotonic()
        self.hold_large(client)
        self.idle()
        self.assertLess(time.monotonic() - started, 4.0)
        client.close()

    def test_cancel_during_partial_delivery_closes_without_replaying(self):
        self.configured_budget()
        client = self.client()
        self.hold_large(client)
        before = self.cluster.scrape()
        self.cancel(client)
        after = self.idle()
        self.assertEqual(
            after.value("read_retries_total"), before.value("read_retries_total")
        )
        self.assertEqual(after.total("queries_total", outcome="delivery_error"), 1)
        client.close()

    def test_10_clients_mixed_sizes_saturation_and_small_query_progress(self):
        self.mixed(10)

    def test_100_clients_mixed_sizes_saturation_and_small_query_progress(self):
        self.mixed(100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
