#!/usr/bin/env python3
"""PERF-008 slice 2: owned real PG saturation, queue cancellation and cleanup.

All 10/100/1,000 frontends are provisioned; no feature switches or skipped cases.
"""
import concurrent.futures
import contextlib
import io
import json
import os
import socket
import struct
import subprocess
import time
import unittest
from pathlib import Path

import failover as f
from metrics_support import MetricsCluster
from pool_support import Client, PoolCase


class Queues(PoolCase):
    evidence_env = 'QUEUE_POOL_EVIDENCE'
    pool_mode = 'transaction'

    def waiters(self, count):
        return f.eventually(lambda: self.scrape().value('pool_waiters') == count,
                            description=f'exactly {count} owned pool waiters')

    def quiet_pool(self, leased=0):
        def quiet():
            snapshot = self.scrape()
            if (snapshot.value('pool_waiters') == 0
                    and snapshot.value('pool_backends', phase='leased') == leased
                    and snapshot.value('pool_backends', phase='resetting') == 0
                    and snapshot.value('pool_backends', phase='connecting') == 0):
                return snapshot
            return None
        result = f.eventually(quiet, description='pool ownership phases quiescent')
        self.assertEqual(result.value('pool_backend_reservations'),
                         result.total('pool_backends'))
        return result

    def hold(self, **parameters):
        clients = [self.client(**parameters) for _ in range(2)]
        for client in clients:
            client.query('BEGIN')
        self.assertNotEqual(self.pid(clients[0]), self.pid(clients[1]))
        return clients

    def physical(self, user='postgres'):
        return int(f.sql(self.fixture.nodes[0], "SELECT count(*) FROM pg_stat_activity "
                         f"WHERE backend_type='client backend' AND usename='{user}' AND pid<>pg_backend_pid()"))

    def saturation(self, count):
        observations = []
        for cycle in range(3):
            clients = [self.client() for _ in range(count)]
            self.assertEqual(self.scrape().value('clients_active'), count)
            for client in clients[:2]:
                client.query('BEGIN')
            self.assertNotEqual(self.pid(clients[0]), self.pid(clients[1]))
            before = self.scrape()
            clients[2].send_query("INSERT INTO pool_data VALUES (2,'canceled-before-dispatch')")
            clients[3].send_query("INSERT INTO pool_data VALUES (3,'abandoned-before-dispatch')")
            for index, client in enumerate(clients[4:], 4):
                client.send_query(f'SELECT {index},{cycle}')
            self.waiters(count - 2)
            held = self.scrape()
            self.assertEqual(held.value('pool_backends', phase='leased'), 2)
            self.assertEqual(held.value('pool_backend_reservations'), 2)
            self.assertEqual(self.physical(), 2)
            self.assertEqual(held.total('query_attempts_total'), before.total('query_attempts_total'))
            self.cancel(clients[2])
            with self.assertRaises(f.PgError) as error:
                clients[2].read_cycle()
            self.assertEqual(error.exception.code, '57014')
            self.assertEqual(clients[2].transaction_status, b'I')
            self.waiters(count - 3)
            # Extra complete messages must not hide EOF while the first is queued.
            clients[3].send_query("INSERT INTO pool_data VALUES (3003,'pipeline-abandoned')")
            clients[3].send_query("INSERT INTO pool_data VALUES (4003,'pipeline-abandoned')")
            clients[3].close()
            self.waiters(count - 4)
            removed = self.scrape()
            self.assertEqual(removed.total('query_attempts_total'), before.total('query_attempts_total'))
            clients[0].query('ROLLBACK')
            for index, client in enumerate(clients[4:], 4):
                self.assertEqual(client.read_cycle(), [[str(index), str(cycle)]])
            self.assertEqual(clients[2].query(f'SELECT 978,{cycle}'), [['978', str(cycle)]])
            quiet = self.quiet_pool(leased=1)
            self.assertEqual(quiet.value('pool_backend_reservations'), 2)
            self.assertEqual(f.sql(self.fixture.nodes[0], 'SELECT count(*) FROM pool_data'), '0')
            clients[1].query('ROLLBACK')
            for client in clients:
                client.close()
            self.idle()
            final = self.quiet_pool()
            self.assertEqual(final.value('pool_backend_reservations'), 2)
            self.assertEqual(self.physical(), 2)
            observations.append({'cycle': cycle, 'actual_frontends': count, 'held_backends': 2,
                                 'maximum_waiters': count - 2, 'final_waiters': final.value('pool_waiters'),
                                 'final_reservations': final.value('pool_backend_reservations'),
                                 'cancelled_and_disconnected_writes': 'independently absent; zero dispatch attempts before release',
                                 'all_surviving_results': 'exact client/cycle values; no retries'})
        (self.output / 'saturation.json').write_text(json.dumps(observations, indent=2) + '\n')

    def test_10_clients_cancel_disconnect_and_repeated_saturation(self):
        self.saturation(10)

    def test_100_clients_cancel_disconnect_and_repeated_saturation(self):
        self.saturation(100)

    def test_1000_clients_cancel_disconnect_and_repeated_saturation(self):
        self.saturation(1000)

    def maximum_clients(self, count):
        self.launch(max_clients=count)
        clients = [self.client() for _ in range(count)]
        self.assertEqual(self.scrape().value('clients_active'), count)
        with self.assertRaises(f.PgError) as error:
            self.connect()
        self.assertEqual(error.exception.code, '53300')
        clients[0].close()
        f.eventually(lambda: self.scrape().value('clients_active') == count - 1)
        with contextlib.closing(self.connect()) as replacement:
            self.assertEqual(replacement.query('SELECT 72'), [['72']])
        for client in clients:
            client.close()
        self.idle()
        self.quiet_pool()

    def test_10_frontend_admission_limit(self):
        self.maximum_clients(10)

    def test_100_frontend_admission_limit(self):
        self.maximum_clients(100)

    def test_1000_frontend_admission_limit(self):
        self.maximum_clients(1000)

    def test_wait_deadline_is_terminal_without_read_retry_or_dispatch(self):
        self.launch(wait_timeout_ms=500)
        holders = self.hold()
        waiting = self.client()
        before = self.scrape()
        started = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(waiting.query, 'SELECT 73')
            self.waiters(1)
            with self.assertRaises(f.PgError) as error:
                future.result(timeout=3)
        elapsed = time.monotonic() - started
        self.assertEqual(error.exception.code, '53300')
        self.assertGreaterEqual(elapsed, .35)
        self.assertLessEqual(elapsed, 1.5)
        self.waiters(0)
        after = self.scrape()
        self.assertEqual(after.value('read_retries_total'), before.value('read_retries_total'))
        self.assertEqual(after.total('query_attempts_total'), before.total('query_attempts_total'))
        for holder in holders:
            holder.query('ROLLBACK')
        self.assertEqual(self.client().query('SELECT 74'), [['74']])
        (self.output / 'wait-deadline.json').write_text(json.dumps({'configured_seconds': .5,
            'observed_seconds': elapsed, 'tolerance': [.35, 1.5], 'sqlstate': error.exception.code,
            'retries': 0, 'dispatches': 0}, indent=2) + '\n')

    def test_fair_queue_order_observed_before_each_capacity_handoff(self):
        holders = self.hold()
        waiting = [self.client() for _ in range(4)]
        for index, client in enumerate(waiting):
            client.send_query('BEGIN')
            self.waiters(index + 1)
        holders[0].query('ROLLBACK')
        for index, client in enumerate(waiting):
            self.assertEqual(client.read_cycle(), [])
            self.assertEqual(client.transaction_status, b'T')
            self.waiters(3 - index)
            self.assertEqual(self.scrape().value('pool_backends', phase='leased'), 2)
            client.query('ROLLBACK')
        holders[1].query('ROLLBACK')

    def test_reserve_creation_waits_for_delay_and_respects_global_limit(self):
        self.launch(reserve_pool_size=1, max_pool_backends=3, reserve_pool_delay_ms=500)
        holders = self.hold()
        extra = self.client()
        started = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(extra.query, 'BEGIN')
            self.waiters(1)
            self.assertEqual(self.scrape().value('pool_backend_reservations'), 2)
            self.assertEqual(future.result(timeout=3), [])
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, .35)
        self.assertLessEqual(elapsed, 1.5)
        self.assertEqual(self.scrape().value('pool_backend_reservations'), 3)
        self.assertEqual(self.physical(), 3)
        following = self.client()
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(following.query, 'SELECT 75')
            self.waiters(1)
            self.assertEqual(self.scrape().value('pool_backend_reservations'), 3)
            extra.query('ROLLBACK')
            self.assertEqual(future.result(timeout=3), [['75']])
        for holder in holders:
            holder.query('ROLLBACK')
        (self.output / 'reserve.json').write_text(json.dumps({'base': 2, 'reserve': 1,
            'global_limit': 3, 'creation_delay_seconds': .5, 'observed_seconds': elapsed,
            'maximum_observed_postgres_connections': 3}, indent=2) + '\n')

    def test_independent_user_and_database_pools_progress_under_saturation(self):
        f.sql(self.fixture.nodes[0], 'CREATE ROLE queued_alice LOGIN; CREATE ROLE independent_bob LOGIN')
        f.sql(self.fixture.nodes[0], 'CREATE DATABASE independent_pool')
        holders = self.hold(user='queued_alice')
        waiting = self.client(user='queued_alice')
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(waiting.query, 'SELECT 76')
            self.waiters(1)
            with contextlib.closing(self.connect(user='independent_bob', database='independent_pool')) as other:
                self.assertEqual(other.query('SELECT current_user,current_database()'),
                                 [['independent_bob', 'independent_pool']])
                self.assertEqual(other.query('SELECT 77'), [['77']])
            self.waiters(1)
            self.assertFalse(future.done())
            holders[0].query('ROLLBACK')
            self.assertEqual(future.result(timeout=3), [['76']])
        holders[1].query('ROLLBACK')

    def test_obsolete_reset_releases_capacity_before_its_network_timeout(self):
        cluster = MetricsCluster(self.output / 'streaming-cluster')
        self.addCleanup(cluster.close)
        cluster.start()
        cluster.stop_proxy()
        path = Path(cluster.directory.name) / 'config.toml'
        text = ('pool_mode = "transaction"\npool_size = 1\nmax_pool_backends = 1\n'
                'pool_reset_timeout_ms = 5000\n' + path.read_text()).replace('probe_interval_ms = 100', 'probe_interval_ms = 1000')
        path.write_text(text)
        (self.output / 'reset-fencing-config.toml').write_text(text)
        cluster.process = subprocess.Popen([str(f.BINARY), str(path)], stdout=cluster.log, stderr=cluster.log)
        f.eventually(cluster.ready)
        old = cluster.connect()
        self.addCleanup(old.close)
        old.query('BEGIN')
        old.query("INSERT INTO continuity VALUES (987,'must-not-replay')")
        cluster.pause(0)
        old.close()
        f.eventually(lambda: cluster.scrape().value('pool_backends', phase='resetting') == 1,
                     description='owned rollback reset held by paused PostgreSQL')
        cluster.promote(1)
        f.eventually(lambda: cluster.scrape().value('node_role', node='b', role='primary') == 1)
        started = time.monotonic()
        f.eventually(lambda: cluster.scrape().value('pool_backend_reservations') == 0,
                     seconds=2, description='obsolete reset discarded before five-second network timeout')
        cleanup = time.monotonic() - started
        with contextlib.closing(cluster.connect()) as client:
            client.query('BEGIN')
            self.assertEqual(client.query("SELECT current_setting('cluster_name')"), [['b']])
            client.query("INSERT INTO continuity VALUES (988,'new-primary')")
            client.query('COMMIT')
        self.assertEqual(f.sql(cluster.nodes[1], 'SELECT count(*) FROM continuity WHERE id=987'), '0')
        self.assertEqual(f.sql(cluster.nodes[1], 'SELECT payload FROM continuity WHERE id=988'), 'new-primary')
        (self.output / 'reset-fencing.json').write_text(json.dumps({'reset_network_timeout_seconds': 5,
            'cleanup_after_topology_publication_seconds': cleanup, 'available_new_primary_capacity': True,
            'old_transaction_replayed': False}, indent=2) + '\n')

    def test_queued_bootstrap_continues_on_new_primary_without_client_reconnect(self):
        cluster = MetricsCluster(self.output / 'streaming-cluster')
        self.addCleanup(cluster.close)
        cluster.start()
        cluster.stop_proxy()
        path = Path(cluster.directory.name) / 'config.toml'
        text = 'pool_mode = "transaction"\npool_size = 2\nmax_pool_backends = 2\n' + path.read_text()
        path.write_text(text)
        (self.output / 'promotion-config.toml').write_text(text)
        cluster.process = subprocess.Popen([str(f.BINARY), str(path)], stdout=cluster.log, stderr=cluster.log)
        f.eventually(cluster.ready)
        holders = [cluster.connect(), cluster.connect()]
        for index, holder in enumerate(holders):
            self.addCleanup(holder.close)
            holder.query('BEGIN')
            holder.query(f"INSERT INTO continuity VALUES ({990 + index},'must-not-replay')")
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(Client, cluster.proxy_port, application_name='promotion-bootstrap')
            f.eventually(lambda: cluster.scrape().value('pool_waiters') == 1)
            self.assertFalse(future.done())
            cluster.pause(0)
            cluster.promote(1)
            f.eventually(lambda: cluster.scrape().value('node_role', node='b', role='primary') == 1)
            with contextlib.closing(future.result(timeout=6)) as continued:
                continued.query('BEGIN')
                self.assertEqual(continued.query("SELECT current_setting('cluster_name')"), [['b']])
                continued.query('COMMIT')
        self.assertEqual(f.sql(cluster.nodes[1], 'SELECT count(*) FROM continuity WHERE id IN (990,991)'), '0')
        for holder in holders:
            with self.assertRaises((f.PgError, OSError)):
                holder.query('COMMIT')
            holder.close()
        (self.output / 'promotion.json').write_text(json.dumps({'queued_startup_connections': 1,
            'client_reconnects': 0, 'new_primary': 'b', 'uncommitted_rows_replayed': 0}, indent=2) + '\n')

    def test_bootstrap_disconnect_with_queued_bytes_immediately_refunds_ticket(self):
        self.launch(max_pool_backends=2)
        holders = self.hold()
        before = self.scrape().total('query_attempts_total')
        observations = []
        for cycle in range(20):
            params = (b'user\0postgres\0database\0postgres\0application_name\0'
                      + f'bootstrap-private-{cycle}'.encode() + b'\0\0')
            with socket.create_connection(('127.0.0.1', self.fixture.proxy_port), timeout=3) as connection:
                connection.sendall(struct.pack('!II', len(params) + 8, 196608) + params)
                self.waiters(1)
                for key in (3003, 4003):
                    body = f"INSERT INTO pool_data VALUES ({key},'bootstrap-abandoned')".encode() + b'\0'
                    connection.sendall(b'Q' + struct.pack('!I', len(body) + 4) + body)
                started = time.monotonic()
            f.eventually(lambda: self.scrape().value('pool_waiters') == 0, seconds=1,
                         description='bootstrap EOF ticket removed without waiting for routing timeout')
            observations.append(time.monotonic() - started)
        snapshot = self.scrape()
        self.assertEqual(snapshot.total('query_attempts_total'), before)
        self.assertEqual(snapshot.value('pool_backend_reservations'), 2)
        self.assertNotIn('bootstrap-private', snapshot.text)
        for holder in holders:
            holder.query('ROLLBACK')
            holder.close()
        self.idle()
        self.quiet_pool()
        self.assertEqual(f.sql(self.fixture.nodes[0], 'SELECT count(*) FROM pool_data'), '0')
        (self.output / 'bootstrap-eof.json').write_text(json.dumps({'cycles': 20,
            'deadline_seconds': 1, 'cleanup_seconds': observations, 'late_writes': 0}, indent=2) + '\n')


if __name__ == '__main__':
    evidence = Path(os.environ['QUEUE_POOL_EVIDENCE']).resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Queues))
    print(output.getvalue(), end='')
    (evidence / 'unittest.log').write_text(output.getvalue())
    (evidence / 'result.json').write_text(json.dumps({'tests': result.testsRun, 'failures': len(result.failures),
        'errors': len(result.errors), 'skips': len(result.skipped), 'expected_failures': len(result.expectedFailures),
        'unexpected_successes': len(result.unexpectedSuccesses)}, indent=2) + '\n')
    raise SystemExit(not (result.wasSuccessful() and not result.skipped and not result.expectedFailures))
