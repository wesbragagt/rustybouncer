"""Owned real PostgreSQL fixtures for complete session/transaction milestone suites."""
import contextlib
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
from metrics_support import Snapshot, free_port, request  # noqa: E402

from benchmarks.compare import Fixture, stop  # noqa: E402


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


class PoolCase(unittest.TestCase):
    evidence_env = 'SESSION_POOL_EVIDENCE'
    pool_mode = 'session'

    def setUp(self):
        self.output = Path(os.environ[self.evidence_env]).resolve() / self._testMethodName
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
            'route_selects': True, 'max_clients': 1200, 'pool_mode': self.pool_mode, 'pool_size': 2,
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

    def active(self, query):
        escaped = query.replace("'", "''")
        return f.eventually(lambda: f.sql(self.fixture.nodes[0],
            f"SELECT count(*) FROM pg_stat_activity WHERE query='{escaped}' AND state='active'") == '1')
