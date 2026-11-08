#!/usr/bin/env python3
"""Black-box metrics acceptance. Requires release/debug binary, Docker and promtool.

METRICS_EVIDENCE selects retained full logs/configs/scrapes. No optional cases.
Runtime abort/listener-task fault injection belongs to runtime unit tests.
"""

import concurrent.futures
import contextlib
import io
import json
import select
import socket
import struct
import time
import unittest

import failover as f
from metrics_support import EVIDENCE, MetricsCluster, request


class Metrics(unittest.TestCase):
    def setUp(self):
        self.cluster = MetricsCluster(EVIDENCE / self._testMethodName)
        # Register cleanup before any setup operation can fail.
        self.addCleanup(self.cluster.close)
        self.cluster.start()

    def client(self):
        client = self.cluster.connect()
        self.addCleanup(client.close)
        return client

    def snapshot(self):
        return self.cluster.scrape()

    def test_inventory_freshness_and_static_cardinality(self):
        first = self.cluster.idle()
        self.assertEqual(first.value('routing_enabled'), 1)
        self.assertEqual(first.value('client_capacity'), 256)
        self.assertEqual(first.value('topology_nodes', role='primary'), 1)
        self.assertEqual(first.value('topology_nodes', role='replica'), 2)
        for node in self.cluster.names:
            self.assertEqual(sum(first.value('node_role', node=node, role=role)
                                 for role in ('primary', 'replica', 'unavailable')), 1)
        for kind in ('write', 'transaction', 'stateful', 'unsupported', 'malformed'):
            self.assertEqual(first.total('queries_total', kind=kind), 0)
        for family in ('read_retries_total', 'clients_rejected_total'):
            self.assertEqual(first.value(family), 0)
        changes = first.value('topology_changes_total')
        second = f.eventually(lambda: self.fresher(first))
        self.assertEqual(second.value('topology_changes_total'), changes)
        second.monotonic(first)
        for index in range(12):
            with contextlib.closing(self.cluster.connect()) as client:
                self.assertEqual(client.query(f"SELECT {index} /* sentinel-secret-{index} */"), [[str(index)]])
        final = self.cluster.idle()
        self.assertEqual(final.samples.keys(), first.samples.keys())
        self.assertNotIn('sentinel-secret', final.text)
        self.cluster.pause(2)
        failed = f.eventually(lambda: self.failed_node())
        self.assertGreater(failed.value('probes_total', node='c', outcome='error'), 0)
        f.eventually(lambda: self.fresher(failed))

    def fresher(self, first):
        current = self.snapshot()
        return current if current.value('topology_last_completed_timestamp_seconds') > first.value('topology_last_completed_timestamp_seconds') else None

    def failed_node(self):
        current = self.snapshot()
        return current if current.value('node_role', node='c', role='unavailable') == 1 else None

    def test_q_message_outcomes_and_histograms(self):
        client = self.client()
        before = self.snapshot()
        cases = [
            ('SELECT 17', 'read', 'ok', [['17']]),
            ('WITH x AS (SELECT 18 AS n) SELECT n FROM x', 'read', 'ok', [['18']]),
            ("INSERT INTO continuity VALUES (1, 'metric') RETURNING id", 'write', 'ok', [['1']]),
            ('BEGIN', 'transaction', 'ok', []), ('ROLLBACK', 'transaction', 'ok', []),
            ('BEGIN', 'transaction', 'ok', []), ('COMMIT', 'transaction', 'ok', []),
            ('SET application_name = \'metrics-sentinel\'', 'stateful', 'ok', []),
            ('SELECT 1/0', 'read', 'sql_error', None),
            ('SELECT 1; SELECT 1/0', 'stateful', 'sql_error', None),
            ('COPY continuity TO STDOUT', 'unsupported', 'router_error', None),
        ]
        for query, kind, outcome, rows in cases:
            prior = self.snapshot()
            if rows is None:
                with self.assertRaises(f.PgError):
                    client.query(query)
            else:
                self.assertEqual(client.query(query), rows)
            after = self.snapshot()
            self.assertEqual(after.total('queries_total') - prior.total('queries_total'), 1)
            self.assertEqual(after.value('queries_total', kind=kind, outcome=outcome)
                             - prior.value('queries_total', kind=kind, outcome=outcome), 1)
            self.assertEqual(after.value('query_duration_seconds_count') - prior.value('query_duration_seconds_count'), 1)
            if kind != 'unsupported':
                self.assertEqual(after.total('query_attempts_total', outcome=outcome)
                                 - prior.total('query_attempts_total', outcome=outcome), 1)
        after = self.snapshot()
        self.assertGreater(after.value('query_duration_seconds_sum'), before.value('query_duration_seconds_sum'))
        self.assertGreater(after.value('route_wait_duration_seconds_count'), before.value('route_wait_duration_seconds_count'))
        after.monotonic(before)
        client.close()
        self.reconcile()

    def reconcile(self):
        current = self.cluster.idle()
        self.assertEqual(current.value('clients_accepted_total'), current.value('clients_rejected_total') + current.total('sessions_completed_total'))
        self.assertEqual(current.value('session_duration_seconds_count'), current.total('sessions_completed_total'))
        # Direct-verifier's own active query and discovery probes are excluded.
        for node in self.cluster.nodes:
            if self.cluster.nodes.index(node) in self.cluster.paused:
                continue
            f.eventually(lambda node=node: f.sql(node, "SELECT count(*) FROM pg_stat_activity WHERE "
                         "backend_type='client backend' AND pid<>pg_backend_pid() AND state='idle'") == '0',
                         description='no idle frontend-owned PostgreSQL sessions')
        return current

    def test_connection_capacity_startup_and_independent_backends(self):
        self.cluster.restart(capacity=2)
        baseline = self.cluster.idle()
        one, two = self.client(), self.client()
        self.assertEqual(one.query('SELECT 41'), [['41']])
        self.assertEqual(two.query('SELECT 42'), [['42']])
        current = self.snapshot()
        self.assertEqual(current.value('clients_active'), 2)
        # Cached streams are idle; probes complete their own sessions per round.
        idle = sum(int(f.sql(node, "SELECT count(*) FROM pg_stat_activity WHERE "
                   "backend_type='client backend' AND pid<>pg_backend_pid() AND state='idle'"))
                   for node in self.cluster.nodes)
        self.assertEqual(current.total('backend_connections_active'), idle)
        with self.assertRaises((f.PgError, OSError)):
            self.cluster.connect()
        one.close()
        two.close()
        after = self.reconcile()
        self.assertEqual(after.value('clients_rejected_total') - baseline.value('clients_rejected_total'), 1)
        # Accepted but malformed startup is a completed error session.
        before = after.total('sessions_completed_total', outcome='error')
        with socket.create_connection(('127.0.0.1', self.cluster.proxy_port), timeout=3) as sock:
            sock.sendall(struct.pack('!II', 8, 1234))
            with contextlib.suppress(OSError):
                sock.recv(4096)
        after = self.reconcile()
        self.assertEqual(after.total('sessions_completed_total', outcome='error') - before, 1)

    def test_terminal_sessions_clean_disconnect_and_unavailable_startup(self):
        before = self.cluster.idle()
        client = self.client()
        client.sock.sendall(b'X' + struct.pack('!I', 4))
        client.close()
        after = self.cluster.idle()
        self.assertEqual(after.value('sessions_completed_total', outcome='ok')
                         - before.value('sessions_completed_total', outcome='ok'), 1)
        # Fully accepted startup socket abandoned before its startup packet.
        with socket.create_connection(('127.0.0.1', self.cluster.proxy_port), timeout=3):
            pass
        abandoned = self.cluster.idle()
        self.assertEqual(abandoned.total('sessions_completed_total') - after.total('sessions_completed_total'), 1)
        for index in range(3):
            self.cluster.pause(index)
        f.eventually(lambda: self.snapshot().value('topology_nodes', role='unavailable') == 3)
        before = self.cluster.idle()
        with self.assertRaises((f.PgError, OSError)):
            self.cluster.connect()
        after = self.cluster.idle()
        self.assertEqual(after.value('sessions_completed_total', outcome='error')
                         - before.value('sessions_completed_total', outcome='error'), 1)
        self.assertEqual(after.total('queries_total'), before.total('queries_total'))

    def test_malformed_full_and_truncated_frames(self):
        for body in (b'\xff\0', b'SELECT 1', b'SELECT\0extra\0'):
            client = self.client()
            before = self.snapshot()
            client.sock.sendall(b'Q' + struct.pack('!I', len(body) + 4) + body)
            with self.assertRaises((f.PgError, OSError)):
                client.read_cycle()
            client.close()
            after = self.cluster.idle()
            self.assertEqual(after.value('queries_total', kind='malformed', outcome='router_error')
                             - before.value('queries_total', kind='malformed', outcome='router_error'), 1)
        for frame in (b'Q\0\0\0\x20incomplete', b'Q' + struct.pack('!I', 0x7fffffff)):
            client = self.client()
            before = self.snapshot()
            client.sock.sendall(frame)
            client.close()
            after = self.cluster.idle()
            self.assertEqual(after.total('queries_total'), before.total('queries_total'))
        self.reconcile()

    def active_node(self, query):
        escaped = query.replace("'", "''")
        def find():
            for index, node in enumerate(self.cluster.nodes):
                if f.sql(node, f"SELECT count(*) FROM pg_stat_activity WHERE query='{escaped}' AND state='active'") == '1':
                    return index + 1
            return None
        return f.eventually(find) - 1

    def test_actual_backend_retry_promotion_and_transaction_nonreplay(self):
        primary = self.cluster.nodes[0]
        f.sql(primary, 'CREATE VIEW metrics_slow AS SELECT 73 AS n FROM pg_sleep(2)')
        for node in self.cluster.nodes[1:]:
            f.eventually(lambda node=node: f.sql(node, "SELECT count(*) FROM pg_class WHERE relname='metrics_slow'") == '1')
        client = self.client()
        before = self.snapshot()
        query = 'SELECT n FROM metrics_slow'
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(client.query, query)
            active = self.active_node(query)
            self.assertEqual(self.snapshot().value('queries_in_flight'), 1)
            f.sql(self.cluster.nodes[active], "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                  f"WHERE query='{query}' AND state='active'")
            self.assertEqual(future.result(timeout=10), [['73']])
        after = self.snapshot()
        self.assertEqual(after.total('queries_total') - before.total('queries_total'), 1)
        self.assertGreater(after.value('read_retries_total'), before.value('read_retries_total'))
        self.assertGreater(after.total('query_attempts_total', outcome='error'), before.total('query_attempts_total', outcome='error'))
        self.assertEqual(after.total('query_attempts_total', outcome='aborted'), before.total('query_attempts_total', outcome='aborted'))
        client.query('BEGIN')
        client.query("INSERT INTO continuity VALUES (91, 'never-replay')")
        before = self.snapshot()
        self.cluster.pause(0)
        self.cluster.promote()
        with self.assertRaises((f.PgError, OSError)):
            client.query('COMMIT')
        f.eventually(lambda: self.snapshot().value('node_role', node='b', role='primary') == 1)
        after = self.snapshot()
        self.assertGreater(after.value('primary_changes_total'), before.value('primary_changes_total'))
        self.assertEqual(f.sql(self.cluster.nodes[1], 'SELECT count(*) FROM continuity WHERE id=91'), '0')
        client.close()
        self.reconcile()

    def test_dispatched_write_failure_is_not_replayed(self):
        client = self.client()
        query = "INSERT INTO continuity SELECT 92, 'uncertain' FROM pg_sleep(3)"
        before = self.snapshot()
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(client.query, query)
            self.assertEqual(self.active_node(query), 0)
            self.cluster.pause(0)
            self.cluster.promote()
            with self.assertRaises((f.PgError, OSError)):
                future.result(timeout=10)
        after = self.snapshot()
        self.assertEqual(after.value('queries_total', kind='write', outcome='router_error')
                         - before.value('queries_total', kind='write', outcome='router_error'), 1)
        self.assertEqual(after.value('read_retries_total'), before.value('read_retries_total'))
        self.assertEqual(f.sql(self.cluster.nodes[1], 'SELECT count(*) FROM continuity WHERE id=92'), '0')
        client.close()
        self.reconcile()

    def test_delivery_failure_and_concurrent_client_results(self):
        clients = [self.client() for _ in range(10)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(client.query, f'SELECT {index}') for index, client in enumerate(clients)]
            for index, future in enumerate(futures):
                self.assertEqual(future.result(timeout=10), [[str(index)]])
        for client in clients:
            client.close()
        before = self.cluster.idle()
        client = self.client()
        client.send_query("SELECT repeat('x',1000000) FROM generate_series(1,8), pg_sleep(1)")
        f.eventually(lambda: self.snapshot().value('queries_in_flight') == 1)
        client.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
        client.close()
        after = self.reconcile()
        self.assertEqual(after.total('queries_total', outcome='delivery_error') - before.total('queries_total', outcome='delivery_error'), 1)

    def test_default_disabled_bind_failure_and_passthrough(self):
        self.cluster.restart(enabled=False)
        with self.assertRaises(OSError):
            request(self.cluster.metrics_port)
        self.assertEqual(self.client().query('SELECT 29'), [['29']])
        self.cluster.stop_proxy()
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', self.cluster.metrics_port))
            occupied.listen()
            self.cluster.restart(wait=False)
            process = self.cluster.process
            assert process is not None
            self.assertNotEqual(process.wait(timeout=5), 0)
            with self.assertRaises(OSError):
                socket.create_connection(('127.0.0.1', self.cluster.proxy_port), timeout=1)
        self.cluster.restart(routed=False)
        before = self.cluster.idle()
        client = self.client()
        self.assertEqual(client.query('SELECT 29'), [['29']])
        self.assertGreater(self.snapshot().total('backend_connections_active'), 0)
        client.close()
        after = self.reconcile()
        self.assertEqual(after.value('routing_enabled'), 0)
        for family in ('queries_total', 'query_attempts_total', 'read_retries_total', 'query_duration_seconds_count', 'route_wait_duration_seconds_count'):
            self.assertEqual(after.total(family), 0)
        self.assertGreater(after.value('clients_accepted_total'), before.value('clients_accepted_total'))

    def test_http_rejections_deadlines_cap_and_sql_availability(self):
        self.assertEqual(request(self.cluster.metrics_port, path='/unknown')[0], 404)
        self.assertEqual(request(self.cluster.metrics_port, method='POST')[0], 405)
        for payload in (b'garbage\r\n\r\n', b'GET /metrics HTTP/1.1\r\nX: ' + b'x' * 9000 + b'\r\n\r\n'):
            with socket.create_connection(('127.0.0.1', self.cluster.metrics_port), timeout=3) as sock:
                sock.sendall(payload)
                with contextlib.suppress(ConnectionResetError):
                    self.assertNotIn(b'200 OK', sock.recv(16384))
        with contextlib.ExitStack() as stack:
            sockets = [stack.enter_context(socket.create_connection(('127.0.0.1', self.cluster.metrics_port), timeout=3)) for _ in range(32)]
            for sock in sockets:
                with contextlib.suppress(OSError):
                    sock.sendall(b'GET /metrics HTTP/1.1\r\nX: ')
            # Excess sockets must close before the two-second read deadline.
            readable, _, _ = select.select(sockets, [], [], .5)
            self.assertGreaterEqual(len(readable), 16, '16-connection cap not enforced')
            self.assertEqual(self.client().query('SELECT 87'), [['87']])
            closed = 0
            started = time.monotonic()
            for sock in sockets:
                try:
                    data = sock.recv(1024)
                    closed += not data or b'200 OK' not in data
                except ConnectionResetError:
                    closed += 1
            self.assertEqual(closed, 32)
            self.assertLess(time.monotonic() - started, 3)
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            results = list(executor.map(lambda _: request(self.cluster.metrics_port), range(16)))
        self.assertTrue(all(status == 200 for status, _, _ in results))
        # No keepalive even when explicitly requested.
        with socket.create_connection(('127.0.0.1', self.cluster.metrics_port), timeout=3) as sock:
            sock.sendall(b'GET /metrics HTTP/1.1\r\nHost: localhost\r\nConnection: keep-alive\r\n\r\n')
            while sock.recv(65536):
                pass
        self.reconcile()


def main():
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Metrics))
    (EVIDENCE / 'unittest.log').write_text(output.getvalue())
    report = {'tests': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
              'skipped': len(result.skipped), 'expected_failures': len(result.expectedFailures),
              'unexpected_successes': len(result.unexpectedSuccesses)}
    (EVIDENCE / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    print(output.getvalue())
    return int(not result.wasSuccessful() or bool(result.skipped or result.expectedFailures))


if __name__ == '__main__':
    raise SystemExit(main())
