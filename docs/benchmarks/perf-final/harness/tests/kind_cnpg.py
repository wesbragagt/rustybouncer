"""H06/F11/D04/G07: six required tests on an owned kind + CloudNativePG cluster."""
import argparse
import concurrent.futures
import re
import threading
import time
import unittest
import urllib.request
import uuid
from pathlib import Path

import psycopg

from conformance.reporting import Result
from tests.kind_cluster import KindCluster, wait_for

OUTPUT = Path('/tmp') / ('rustybouncer-kind-' + uuid.uuid4().hex[:10])
IMAGE = 'rustybouncer:latest'
IDENTITY = 'SELECT inet_server_addr()::text, pg_is_in_recovery()'


def metric(text, name, **labels):
    values = []
    for line in text.splitlines():
        match = re.fullmatch(r'rustybouncer_' + re.escape(name) + r'(?:\{([^}]*)\})? ([0-9.eE+-]+)', line)
        if match:
            actual = dict(re.findall(r'(\w+)="([^"]*)"', match[1] or ''))
            if all(actual.get(key) == value for key, value in labels.items()):
                values.append(float(match[2]))
    if not values:
        raise AssertionError(f'missing metric {name} {labels}')
    return sum(values)


class CloudNativePG(unittest.TestCase):
    cluster: KindCluster

    @classmethod
    def setUpClass(cls):
        cls.cluster = KindCluster(OUTPUT, IMAGE)
        cls.addClassCleanup(cls.cluster.close)
        cls.cluster.start()

    def setUp(self):
        self.cluster.healthy()
        self.evidence = []
        self.clients = []
        self.schema = 'kind_' + uuid.uuid4().hex
        self.table = self.schema + '.records'
        self.addCleanup(self.drop_schema)
        self.cluster.sql(f'CREATE SCHEMA {self.schema} AUTHORIZATION app')
        self.cluster.sql(f'CREATE TABLE {self.table} (id int PRIMARY KEY, value text); ALTER TABLE {self.table} OWNER TO app')
        # A replication barrier prevents visibility lag being mistaken for bad routing.
        for pod in self.cluster.pods():
            name = pod['metadata']['name']
            wait_for(lambda name=name: self.cluster.sql(f"SELECT to_regclass('{self.table}') IS NOT NULL", name) == 't',
                     f'{self.table} replayed on {name}')

    def drop_schema(self):
        for client in self.clients:
            client.close()
        self.cluster.sql(f'DROP SCHEMA IF EXISTS {self.schema} CASCADE')

    def client(self):
        client = self.cluster.connect()
        self.clients.append(client)
        return client

    def query(self, client, sql):
        started = time.monotonic()
        rows = client.execute(sql).fetchall()
        self.evidence.append({'query': sql, 'rows': rows, 'duration_s': time.monotonic() - started})
        return rows

    def metrics(self):
        with urllib.request.urlopen(f'http://127.0.0.1:{self.cluster.metrics_port}/metrics', timeout=3) as response:
            self.assertEqual(response.status, 200)
            return response.read().decode()

    def test_metrics_account_for_replica_queries_and_topology(self):
        client = self.client()
        before = self.metrics()
        self.cluster.run('promtool', 'check', 'metrics', data=before)
        self.assertEqual(metric(before, 'routing_enabled'), 1)
        self.assertEqual(metric(before, 'topology_nodes', role='primary'), 1)
        self.assertEqual(metric(before, 'topology_nodes', role='replica'), 2)
        primary = self.cluster.primary()
        for pod in self.cluster.pods():
            name = pod['metadata']['name']
            self.assertEqual(metric(before, 'node_role', node=name,
                                    role='primary' if name == primary else 'replica'), 1)
        for sql in ['SELECT 666', 'WITH item AS (SELECT 667 AS n) SELECT n FROM item']:
            self.assertEqual(self.query(client, sql), [(666 if sql.startswith('SELECT') else 667,)])
        after = self.metrics()
        self.cluster.run('promtool', 'check', 'metrics', data=after)
        self.assertEqual(metric(after, 'queries_total', kind='read', outcome='ok')
                         - metric(before, 'queries_total', kind='read', outcome='ok'), 2)
        self.assertEqual(metric(after, 'query_attempts_total', route='replica', outcome='ok')
                         - metric(before, 'query_attempts_total', route='replica', outcome='ok'), 2)
        self.assertGreater(metric(after, 'backend_connections_active'), 0)
        (self.cluster.output / 'metrics-before.prom').write_text(before)
        (self.cluster.output / 'metrics-after.prom').write_text(after)
        client.close()
        wait_for(lambda: metric(self.metrics(), 'clients_active') == 0
                 and metric(self.metrics(), 'backend_connections_active') == 0,
                 'metrics gauges to return to zero after the frontend closes')
        self.cluster.record('metrics-validated', primary=primary, read_messages=2, replica_attempts=2)

    def test_primary_writes(self):
        primary = self.cluster.primary()
        ip = next(pod['status']['podIP'] for pod in self.cluster.pods() if pod['metadata']['name'] == primary)
        rows = self.query(self.client(), f"INSERT INTO {self.table} VALUES (1, 'primary') RETURNING inet_server_addr()::text")
        self.assertEqual([row[0].split('/')[0] for row in rows], [ip])
        self.assertEqual(self.cluster.sql(f'SELECT count(*) FROM {self.table} WHERE id=1'), '1')

    def test_replica_round_robin_and_ctes(self):
        primary = self.cluster.primary()
        replicas = {pod['status']['podIP'] for pod in self.cluster.pods() if pod['metadata']['name'] != primary}
        client = self.client()
        for sql in [IDENTITY, f'WITH identity AS ({IDENTITY}) SELECT * FROM identity']:
            rows = [self.query(client, sql)[0] for _ in range(6)]
            self.assertEqual({row[0].split('/')[0] for row in rows}, replicas)
            self.assertTrue(all(row[1] for row in rows))
            self.assertTrue(all(rows[i][0] != rows[i + 1][0] for i in range(len(rows) - 1)))

    def test_replica_replacement(self):
        client = self.client()
        replicas = [pod for pod in self.cluster.pods() if pod['metadata']['name'] != self.cluster.primary()]
        victim = replicas[0]
        name, old_uid = victim['metadata']['name'], victim['metadata']['uid']
        for _ in range(4):
            self.query(client, IDENTITY)
        self.cluster.kube('-n', 'rustybouncer-test', 'delete', 'pod', name, '--wait=false')
        def replaced():
            return any(pod['metadata']['name'] == name and pod['metadata']['uid'] != old_uid for pod in self.cluster.pods())
        wait_for(replaced, 'CNPG to replace the deleted replica pod')
        for _ in range(12):
            self.assertTrue(self.query(client, IDENTITY)[0][1])
        self.cluster.healthy()
        self.cluster.record('replica-replaced', pod=name, old_uid=old_uid, pods=self.cluster.pods())
        replica_ips = {pod['status']['podIP'] for pod in self.cluster.pods() if pod['metadata']['name'] != self.cluster.primary()}
        self.assertEqual({self.query(client, IDENTITY)[0][0].split('/')[0] for _ in range(6)}, replica_ips)

    def test_reads_continue_and_writes_follow_node_failover(self):
        client = self.client()
        self.query(client, IDENTITY)
        stop, entered = threading.Event(), threading.Event()
        addresses = {pod['status']['podIP'] for pod in self.cluster.pods()}
        def reads():
            count = 0
            while not stop.is_set():
                rows = self.query(client, 'SELECT 42, inet_server_addr()::text')
                self.assertEqual(rows[0][0], 42)
                self.assertIn(rows[0][1].split('/')[0], addresses)
                count += 1
                entered.set()
                stop.wait(.05)
            return count
        self.addCleanup(self.cluster.restore)
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(reads)
            try:
                self.assertTrue(entered.wait(10), 'read loop did not start')
                old = self.cluster.crash_primary()
                new = self.cluster.promoted(old)
            finally:
                stop.set()
            count = future.result(timeout=15)
        self.assertGreater(count, 1)
        # Reuse the exact same frontend socket after the read loop finishes.
        rows = self.query(client, f"INSERT INTO {self.table} VALUES (2, 'after failover') RETURNING inet_server_addr()::text")
        new_ip = next(pod['status']['podIP'] for pod in self.cluster.pods() if pod['metadata']['name'] == new)
        self.assertEqual([row[0].split('/')[0] for row in rows], [new_ip])
        self.assertEqual(self.cluster.sql(f'SELECT count(*) FROM {self.table} WHERE id=2', new), '1')
        self.cluster.record('continuous-reads', count=count, old_primary=old, new_primary=new)

    def test_transaction_interruption_is_not_replayed(self):
        client = self.client()
        client.execute('BEGIN')
        client.execute(f"INSERT INTO {self.table} VALUES (3, 'uncommitted')")
        self.addCleanup(self.cluster.restore)
        previous = self.cluster.crash_primary()
        new = self.cluster.promoted(previous)
        with self.assertRaises((psycopg.OperationalError, psycopg.InterfaceError)):
            client.execute('COMMIT')
        self.assertEqual(self.cluster.sql(f'SELECT count(*) FROM {self.table} WHERE id=3', new), '0')
        self.cluster.record('transaction-not-replayed', previous=previous, primary=new, rows=0)


def main():
    global OUTPUT, IMAGE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=OUTPUT)
    parser.add_argument('--image', default=IMAGE, help='locally loaded Docker image; imported into this kind cluster')
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output directory already exists; choose a new path to preserve prior evidence')
    OUTPUT, IMAGE = args.output, args.image
    try:
        result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(CloudNativePG))
    finally:
        # Also dispose owned resources when the user interrupts class setup.
        CloudNativePG.doClassCleanups()
    assert isinstance(result, Result)
    result.write(OUTPUT / 'results.json', OUTPUT / 'junit.xml',
                 {'suite': 'kind-cnpg', 'image': IMAGE, 'pins': getattr(getattr(CloudNativePG, 'cluster', None), 'pins', {})})
    return int(not result.wasSuccessful())


if __name__ == '__main__':
    raise SystemExit(main())
