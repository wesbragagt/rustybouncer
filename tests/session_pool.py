#!/usr/bin/env python3
"""PERF-007: real PostgreSQL session ownership, reset, identity and idle/churn."""
import concurrent.futures
import contextlib
import io
import json
import os
import socket
import struct
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import failover as f  # noqa: E402
from metrics_support import MetricsCluster, Snapshot, free_port, request  # noqa: E402

from benchmarks.compare import Fixture, stop  # noqa: E402

EVIDENCE = Path(os.environ['SESSION_POOL_EVIDENCE']).resolve()


class Client(f.Pg):
    def __init__(self, port, **parameters):
        self.sock = socket.create_connection(('127.0.0.1', port), timeout=12)
        values = {'user': 'postgres', 'database': 'postgres', **parameters}
        body = b''.join(k.encode() + b'\0' + v.encode() + b'\0' for k, v in values.items()) + b'\0'
        self.sock.sendall(struct.pack('!II', len(body) + 8, 196608) + body)
        try:
            self.read_cycle()
        except BaseException:
            self.close()
            raise


class SessionPool(unittest.TestCase):
    def setUp(self):
        self.output = EVIDENCE / self._testMethodName
        self.output.mkdir(parents=True)
        self.fixture = Fixture(self.output, f.BINARY)
        self.addCleanup(self.fixture.close)
        self.fixture.start()
        f.sql(self.fixture.nodes[0], 'CREATE TABLE pool_data(id int primary key, value text)')
        self.metrics_port = free_port()
        self.launch()

    def launch(self, **settings):
        stop(self.fixture.process)
        if self.fixture.log:
            self.fixture.log.close()
        self.fixture.proxy_port = free_port()
        options = {
            'listen': f'127.0.0.1:{self.fixture.proxy_port}', 'metrics_listen': f'127.0.0.1:{self.metrics_port}',
            'route_selects': True, 'max_clients': 1200, 'pool_mode': 'session', 'pool_size': 2,
            'max_pool_backends': 8, 'pool_idle_timeout_ms': 30000, 'pool_server_lifetime_ms': 300000,
            'pool_reset_timeout_ms': 2000, 'probe_interval_ms': 100, 'probe_timeout_ms': 300,
            'wait_timeout_ms': 8000, 'startup_timeout_ms': 10000, 'query_timeout_ms': 10000,
            'probe_user': 'comparison_probe', 'probe_database': 'postgres', **settings,
        }
        text = ''.join(f'{key} = {json.dumps(value)}\n' for key, value in options.items())
        text += f'[[nodes]]\nname = "primary"\nhost = "127.0.0.1"\nport = {self.fixture.pg_port}\n'
        path = self.output / f'config-{time.time_ns()}.toml'
        path.write_text(text)
        self.fixture.log = (self.output / f'proxy-{time.time_ns()}.log').open('w')
        self.fixture.process = subprocess.Popen([str(f.BINARY), str(path)], stdout=self.fixture.log, stderr=self.fixture.log)
        f.eventually(lambda: self.scrape().value('topology_discovery_completed') == 1)
        with contextlib.closing(self.connect()) as client:
            self.assertEqual(client.query('SELECT 1'), [['1']])
        self.idle()

    def connect(self, **parameters):
        return Client(self.fixture.proxy_port, **parameters)

    def client(self, **parameters):
        client = self.connect(**parameters)
        self.addCleanup(client.close)
        return client

    def scrape(self):
        status, _, text = request(self.metrics_port)
        assert status == 200
        result = Snapshot(text)
        result.validate(['primary'])
        return result

    def idle(self):
        def quiet():
            snapshot = self.scrape()
            if snapshot.value('clients_active') == snapshot.value('queries_in_flight') == 0:
                return snapshot
            return None
        snapshot = f.eventually(quiet, seconds=12, description='all frontends/reset owners completed')
        self.assertEqual(snapshot.value('result_buffer_reserved_bytes'), 0)
        (self.output / f'idle-{time.time_ns()}.prom').write_text(snapshot.text)
        return snapshot

    @staticmethod
    def pid(client):
        return int(client.query('SELECT pg_backend_pid()')[0][0])

    def cancel(self, client, key=None):
        with socket.create_connection(('127.0.0.1', self.fixture.proxy_port), timeout=3) as connection:
            connection.sendall(struct.pack('!IIII', 16, 80877102, *(key or client.cancel_key)))
            self.assertEqual(connection.recv(1), b'')

    def test_session_affinity_exclusive_ownership_and_reuse(self):
        a, b = self.client(), self.client()
        first, second = self.pid(a), self.pid(b)
        self.assertNotEqual(first, second)
        self.assertEqual(self.pid(a), first)
        a.close()
        f.eventually(lambda: self.scrape().value('clients_active') == 1)
        c = self.client()
        self.assertEqual(self.pid(c), first)
        self.assertEqual(self.pid(b), second)
        self.assertGreater(self.scrape().value('pool_reuses_total'), 0)

    def test_reset_temporary_state_sql_prepared_locks_listen_and_settings(self):
        a = self.client()
        pid = self.pid(a)
        a.query("SET application_name='dirty'; SET search_path=pg_catalog; CREATE TEMP TABLE leaked(id int); "
                "PREPARE leaked_plan AS SELECT 7; SELECT pg_advisory_lock(137904); LISTEN pool_leak")
        a.close()
        self.idle()
        b = self.client()
        self.assertEqual(self.pid(b), pid)
        self.assertEqual(b.query("SELECT current_setting('application_name'),current_setting('search_path'),"
                                "to_regclass('pg_temp.leaked')"), [['', '"$user", public', None]])
        self.assertEqual(b.query("SELECT count(*) FROM pg_prepared_statements WHERE name='leaked_plan'"), [['0']])
        self.assertEqual(b.query('SELECT count(*) FROM pg_listening_channels()'), [['0']])
        self.assertEqual(b.query('SELECT pg_try_advisory_lock(137904)'), [['t']])
        b.query('SELECT pg_advisory_unlock(137904)')
        with self.assertRaises(f.PgError) as error:
            b.query('EXECUTE leaked_plan')
        self.assertEqual(error.exception.code, '26000')
        self.assertEqual(b.query('SELECT 91'), [['91']])

    def test_disconnect_open_transaction_rolls_back_before_reuse(self):
        a = self.client()
        pid = self.pid(a)
        a.query('BEGIN')
        a.query("INSERT INTO pool_data VALUES (1,'uncommitted')")
        self.assertEqual(f.sql(self.fixture.nodes[0], 'SELECT count(*) FROM pool_data'), '0')
        a.close()
        self.idle()
        b = self.client()
        self.assertEqual(self.pid(b), pid)
        self.assertEqual(b.query('SELECT count(*) FROM pool_data'), [['0']])
        self.assertEqual(b.transaction_status, b'I')

    def test_disconnect_aborted_transaction_does_not_contaminate_next_client(self):
        a = self.client()
        pid = self.pid(a)
        a.query('BEGIN')
        a.query("INSERT INTO pool_data VALUES (2,'discarded')")
        with self.assertRaises(f.PgError):
            a.query('SELECT 1/0')
        self.assertEqual(a.transaction_status, b'E')
        a.close()
        self.idle()
        b = self.client()
        self.assertEqual(self.pid(b), pid)
        self.assertEqual(b.query('SELECT count(*) FROM pool_data'), [['0']])
        self.assertEqual(b.query('SELECT 92'), [['92']])

    def test_user_and_database_identity_never_uses_privileged_probe(self):
        f.sql(self.fixture.nodes[0], 'CREATE ROLE pool_alice LOGIN; CREATE ROLE pool_bob LOGIN; '
              'CREATE TABLE private_pool(value int); REVOKE ALL ON private_pool FROM PUBLIC')
        f.sql(self.fixture.nodes[0], 'CREATE DATABASE pool_other')
        privileged = self.client()
        original_pid = self.pid(privileged)
        privileged.query('SET SESSION AUTHORIZATION pool_alice')
        self.assertEqual(privileged.query('SELECT current_user'), [['pool_alice']])
        privileged.close()
        self.idle()
        with contextlib.closing(self.connect()) as restored:
            self.assertEqual(self.pid(restored), original_pid)
            self.assertEqual(restored.query('SELECT current_user,session_user'), [['postgres', 'postgres']])
        self.idle()
        identities = [('pool_alice', 'postgres'), ('pool_bob', 'postgres'), ('pool_alice', 'pool_other')]
        pids = []
        for user, database in identities:
            with contextlib.closing(self.connect(user=user, database=database)) as client:
                pids.append(self.pid(client))
                self.assertEqual(client.query('SELECT current_user,current_database()'), [[user, database]])
                if database == 'postgres':
                    with self.assertRaises(f.PgError) as error:
                        client.query('SELECT * FROM private_pool')
                    self.assertEqual(error.exception.code, '42501')
            self.idle()
        self.assertEqual(len(set(pids)), 3)
        with contextlib.closing(self.connect(user='pool_alice')) as again:
            self.assertEqual(self.pid(again), pids[0])

    def test_startup_parameters_partition_and_restore_original_defaults(self):
        a = self.client(application_name='original-a', options='-c search_path=pg_catalog')
        first = self.pid(a)
        a.query("SET application_name='changed'; SET search_path=public")
        a.close()
        self.idle()
        b = self.client(application_name='original-a', options='-c search_path=pg_catalog')
        self.assertEqual(self.pid(b), first)
        self.assertEqual(b.query("SELECT current_setting('application_name'),current_setting('search_path')"),
                         [['original-a', 'pg_catalog']])
        c = self.client(application_name='original-b', options='-c search_path=public')
        self.assertNotEqual(self.pid(c), first)
        self.assertEqual(c.query("SELECT current_setting('application_name'),current_setting('search_path')"),
                         [['original-b', 'public']])

    def test_stale_virtual_cancel_does_not_hit_next_owner(self):
        a = self.client()
        pid, stale = self.pid(a), a.cancel_key
        a.close()
        self.idle()
        b = self.client()
        self.assertEqual(self.pid(b), pid)
        self.assertNotEqual(b.cancel_key, stale)
        query = 'SELECT 95 FROM pg_sleep(1)'
        b.send_query(query)
        self.active(query)
        self.cancel(b, stale)
        self.assertEqual(b.read_cycle(), [['95']])
        b.send_query('SELECT 96 FROM pg_sleep(30)')
        self.active('SELECT 96 FROM pg_sleep(30)')
        self.cancel(b)
        with self.assertRaises(f.PgError) as error:
            b.read_cycle()
        self.assertEqual(error.exception.code, '57014')
        self.assertEqual(b.query('SELECT 97'), [['97']])

    def active(self, query):
        escaped = query.replace("'", "''")
        return f.eventually(lambda: f.sql(self.fixture.nodes[0],
            f"SELECT count(*) FROM pg_stat_activity WHERE query='{escaped}' AND state='active'") == '1')

    def test_backend_transport_failure_during_reset_discards_connection(self):
        a = self.client()
        pid = self.pid(a)
        before = self.scrape().value('pool_resets_total', outcome='error')
        self.assertEqual(f.sql(self.fixture.nodes[0], f'SELECT pg_terminate_backend({pid})'), 't')
        a.close()
        f.eventually(lambda: self.scrape().value('pool_resets_total', outcome='error') == before + 1)
        self.idle()
        b = self.client()
        self.assertNotEqual(self.pid(b), pid)
        self.assertEqual(b.query('SELECT 98'), [['98']])

    def test_killed_idle_backend_is_retired_without_restarting_pooler(self):
        a = self.client()
        pid = self.pid(a)
        a.close()
        self.idle()
        self.assertEqual(f.sql(self.fixture.nodes[0], f'SELECT pg_terminate_backend({pid})'), 't')
        f.eventually(lambda: self.scrape().value('backend_connections_active', node='primary') == 0)
        b = self.client()
        self.assertNotEqual(self.pid(b), pid)
        self.assertEqual(b.query('SELECT 99'), [['99']])

    def test_idle_and_lifetime_retirement_release_capacity(self):
        self.launch(pool_idle_timeout_ms=500, pool_server_lifetime_ms=2000)
        a = self.client()
        first = self.pid(a)
        a.close()
        self.idle()
        f.eventually(lambda: self.scrape().value('backend_connections_active', node='primary') == 0)
        b = self.client()
        second = self.pid(b)
        self.assertNotEqual(first, second)
        # Lifetime is checked on return; active ownership is never forcibly stolen.
        started = time.monotonic()
        self.assertEqual(b.query('SELECT 100 FROM pg_sleep(2.2)'), [['100']])
        self.assertGreater(time.monotonic() - started, 2)
        b.close()
        self.idle()
        self.assertEqual(self.scrape().value('backend_connections_active', node='primary'), 0)
        self.assertNotIn(self.pid(self.client()), (first, second))

    def test_topology_generation_retires_old_backends(self):
        a = self.client()
        first = self.pid(a)
        before = self.scrape().value('primary_changes_total')
        f.docker('pause', self.fixture.nodes[0])
        try:
            f.eventually(lambda: self.scrape().value('primary_changes_total') > before)
        finally:
            f.docker('unpause', self.fixture.nodes[0])
        f.eventually(lambda: self.scrape().value('topology_nodes', role='primary') == 1)
        with self.assertRaises((f.PgError, OSError)):
            a.query('SELECT 101')
        a.close()
        self.idle()
        self.assertNotEqual(self.pid(self.client()), first)

    def test_physical_promotion_fences_session_and_reuses_only_new_primary(self):
        cluster = MetricsCluster(self.output / 'streaming-cluster')
        self.addCleanup(cluster.close)
        cluster.start()
        cluster.stop_proxy()
        path = Path(cluster.directory.name) / 'config.toml'
        text = 'pool_mode = "session"\npool_size = 2\nmax_pool_backends = 8\n' + path.read_text()
        path.write_text(text)
        (self.output / 'streaming-config.toml').write_text(text)
        cluster.process = subprocess.Popen([str(f.BINARY), str(path)], stdout=cluster.log, stderr=cluster.log)
        f.eventually(cluster.ready)
        a = cluster.connect()
        self.addCleanup(a.close)
        a.query('BEGIN')
        a.query("INSERT INTO continuity VALUES (901,'not-committed')")
        cluster.pause(0)
        cluster.promote(1)
        f.eventually(lambda: cluster.scrape().value('node_role', node='b', role='primary') == 1)
        with self.assertRaises((f.PgError, OSError)):
            a.query('COMMIT')
        a.close()
        self.assertEqual(f.sql(cluster.nodes[1], 'SELECT count(*) FROM continuity WHERE id=901'), '0')
        with contextlib.closing(cluster.connect()) as b:
            new_pid = self.pid(b)
            self.assertEqual(b.query("SELECT current_setting('cluster_name')"), [['b']])
        f.eventually(lambda: cluster.scrape().value('clients_active') == 0)
        with contextlib.closing(cluster.connect()) as c:
            self.assertEqual(self.pid(c), new_pid)
            self.assertEqual(c.query("SELECT current_setting('cluster_name')"), [['b']])

    def test_saturated_session_pool_waits_then_hands_off_exclusively(self):
        a, b, c = self.client(), self.client(), self.client()
        first, second = self.pid(a), self.pid(b)
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(self.pid, c)
            f.eventually(lambda: self.scrape().value('queries_in_flight') == 1)
            self.assertFalse(future.done())
            self.assertEqual(self.scrape().value('backend_connections_active', node='primary'), 2)
            a.close()
            self.assertEqual(future.result(timeout=5), first)
        self.assertEqual(self.pid(b), second)

    def test_idle_frontends_share_startup_metadata_without_backend_leases(self):
        clients = [self.client() for _ in range(100)]
        snapshot = self.scrape()
        self.assertEqual(snapshot.value('clients_active'), 100)
        self.assertEqual(snapshot.value('backend_connections_active', node='primary'), 1)
        self.assertEqual(len({client.cancel_key for client in clients}), 100)
        self.assertEqual(len({self.pid(clients[index]) for index in (0, 1)}), 2)
        (self.output / 'idle-observation.json').write_text(json.dumps({
            'frontends': 100, 'postgres_backends': 1, 'assigned_session_backends_after_two_queries': 2,
            'startup': 'completed for all clients; no application SQL before observation'}, indent=2) + '\n')

    def test_unpooled_churn_opens_one_backend_per_client(self):
        self.launch(pool_mode='none')
        pids = []
        for index in range(50):
            with contextlib.closing(self.connect()) as client:
                pids.append(self.pid(client))
                self.assertEqual(client.query(f'SELECT {index}'), [[str(index)]])
            self.idle()
        self.assertEqual(len(set(pids)), 50)
        (self.output / 'churn.json').write_text(json.dumps({
            'mode': 'none', 'clients': 50, 'queries_per_client': 2,
            'unique_postgres_pids': pids, 'interpretation': 'same sequential ownership oracle as session mode; no throughput claim'},
            indent=2) + '\n')

    def test_declared_sequential_connection_churn_reuses_one_backend(self):
        self.launch(pool_size=1)
        pids, latencies = [], []
        for index in range(50):
            started = time.monotonic()
            with contextlib.closing(self.connect()) as client:
                pids.append(self.pid(client))
                self.assertEqual(client.query(f'SELECT {index}'), [[str(index)]])
            self.idle()
            latencies.append(time.monotonic() - started)
        self.assertEqual(len(set(pids)), 1)
        (self.output / 'churn.json').write_text(json.dumps({
            'clients': 50, 'queries_per_client': 2, 'unique_postgres_pids': sorted(set(pids)),
            'seconds_including_independent_cleanup_barriers': latencies,
            'interpretation': 'correctness/ownership experiment, not throughput timing'}, indent=2) + '\n')


if __name__ == '__main__':
    EVIDENCE.mkdir(parents=True, exist_ok=False)
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(SessionPool))
    print(output.getvalue(), end='')
    (EVIDENCE / 'unittest.log').write_text(output.getvalue())
    (EVIDENCE / 'result.json').write_text(json.dumps({'tests': result.testsRun, 'failures': len(result.failures),
        'errors': len(result.errors), 'skips': len(result.skipped), 'expected_failures': len(result.expectedFailures),
        'unexpected_successes': len(result.unexpectedSuccesses)}, indent=2) + '\n')
    raise SystemExit(not (result.wasSuccessful() and not result.skipped and not result.expectedFailures))
