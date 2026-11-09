#!/usr/bin/env python3
"""PERF-008/009: 144 bounded-pool arms, all 10/100/1,000 actual frontends.

Four session/transaction poolers, two CPU placements, two workloads, three
repetitions. Five-second warmup, ten-second measurement, with bounded drain.
All 144 arms and real prepared/row preflight are mandatory. No performance budget.
"""
import argparse
import asyncio
import contextlib
import hashlib
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks import compare as c  # noqa: E402
from benchmarks.performance_profile import dump, process_info  # noqa: E402
from benchmarks.workload_compare import Observed, expected  # noqa: E402

TARGETS = ('pgbouncer-session', 'pgbouncer-transaction', 'rustybouncer-pool-session', 'rustybouncer-pool-transaction')
WORKLOADS = ('churn', 'transactions')
POOL_SIZE = 16
CLIENT_CAPACITY = 1200

# pgbench checks numeric identity/value guards in every measured transaction.
# The 128-byte payload still crosses the wire. Exact full-byte and prepared
# value oracles run for every concurrently provisioned preflight frontend.
TRANSACTION_SCRIPT = r"""BEGIN;
\set id random(1,10000)
SELECT id AS observed_id, :client_id::int AS observed_client, payload,
       CASE WHEN payload=repeat(md5(:id::text),4) THEN 1 ELSE 0 END AS payload_ok
FROM comparison WHERE id=:id \gset
\if :observed_id != :id OR :observed_client != :client_id OR :payload_ok != 1
SELECT 1/0;
\endif
COMMIT;
"""


class PoolFixture(Observed):
    def launch_target(self, target, directory):
        if self.log:
            self.log.close()
        self.proxy_port = self.available_port()
        mode = 'transaction' if target.endswith('transaction') else 'session'
        if target.startswith('pgbouncer'):
            users = self.output / 'users.txt'
            users.write_text('"postgres" ""\n')
            path = directory / 'pgbouncer.ini'
            path.write_text(f'''[databases]
postgres = host=127.0.0.1 port={self.pg_port} dbname=postgres
[pgbouncer]
listen_addr = 127.0.0.1
listen_port = {self.proxy_port}
unix_socket_dir =
auth_type = trust
auth_file = {users}
admin_users = postgres
pool_mode = {mode}
default_pool_size = {POOL_SIZE}
max_db_connections = {POOL_SIZE}
max_user_connections = {POOL_SIZE}
reserve_pool_size = 0
max_client_conn = {CLIENT_CAPACITY}
query_wait_timeout = 60
server_connect_timeout = 3
server_idle_timeout = 30
server_lifetime = 300
max_prepared_statements = 16
log_connections = 0
log_disconnections = 0
''')
            argv = ['pgbouncer', str(path)]
        else:
            path = directory / 'router.toml'
            path.write_text(f'''listen = "127.0.0.1:{self.proxy_port}"
max_clients = {CLIENT_CAPACITY}
route_selects = true
pool_mode = "{mode}"
pool_size = {POOL_SIZE}
max_pool_backends = {POOL_SIZE}
reserve_pool_size = 0
pool_idle_timeout_ms = 30000
pool_server_lifetime_ms = 300000
pool_reset_timeout_ms = 5000
probe_interval_ms = 500
probe_timeout_ms = 3000
wait_timeout_ms = 60000
startup_timeout_ms = 10000
query_timeout_ms = 30000
probe_user = "comparison_probe"
probe_database = "postgres"
[[nodes]]
name = "primary"
host = "127.0.0.1"
port = {self.pg_port}
''')
            argv = [str(self.binary), str(path)]
        self.log = (directory / 'process.log').open('w')
        self.process = subprocess.Popen(argv, stdout=self.log, stderr=subprocess.STDOUT)
        dump(directory / 'process-command.json', argv)
        c.m.f.eventually(self.ready, description='bounded-pool proxy readiness')

    def ready(self):
        assert self.process is not None
        assert self.process.poll() is None, 'proxy exited during startup'
        with contextlib.closing(self.connect()) as client:
            return client.query('SELECT 1') == [['1']]


class AsyncPg:
    """Real wire client for 1,000-client barriers; never reconnects or retries SQL."""
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.status = b'I'

    @classmethod
    async def connect(cls, port):
        reader, writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', port), 30)
        client = cls(reader, writer)
        try:
            params = b'user\0postgres\0database\0postgres\0\0'
            writer.write(struct.pack('!II', len(params) + 8, 196608) + params)
            await writer.drain()
            assert await asyncio.wait_for(client.cycle(), 90) == []
            return client
        except BaseException:
            await client.close()
            raise

    @staticmethod
    def row(body):
        columns = struct.unpack_from('!H', body)[0]
        result, offset = [], 2
        for _ in range(columns):
            size = struct.unpack_from('!i', body, offset)[0]
            offset += 4
            assert size >= -1 and (size < 0 or offset + size <= len(body))
            result.append(None if size < 0 else body[offset:offset + size].decode())
            offset += max(0, size)
        assert offset == len(body)
        return result

    async def cycle(self):
        rows, error = [], None
        while True:
            tag = await self.reader.readexactly(1)
            length = struct.unpack('!I', await self.reader.readexactly(4))[0]
            assert 4 <= length <= 16 * 1024 * 1024
            body = await self.reader.readexactly(length - 4)
            if tag == b'R':
                assert body == b'\0\0\0\0', 'benchmark fixture must use trust'
            if tag == b'D':
                rows.append(self.row(body))
            if tag == b'E':
                error = c.m.f.PgError(body)
                if error.severity in ('FATAL', 'PANIC'):
                    raise error
            if tag == b'Z':
                assert body in (b'I', b'T', b'E')
                self.status = body
                if error:
                    raise error
                return rows

    async def frames(self, frames):
        for tag, body in frames:
            self.writer.write(tag + struct.pack('!I', len(body) + 4) + body)
        await self.writer.drain()
        return await asyncio.wait_for(self.cycle(), 90)

    async def query(self, sql):
        return await self.frames([(b'Q', sql.encode() + b'\0')])

    async def prepare(self, name, sql):
        body = name + b'\0' + sql.encode() + b'\0' + struct.pack('!HI', 1, 23)
        return await self.frames([(b'P', body), (b'S', b'')])

    async def prepared(self, name, value):
        encoded = str(value).encode()
        bind = b'\0' + name + b'\0' + struct.pack('!HHI', 0, 1, len(encoded)) + encoded + struct.pack('!H', 0)
        return await self.frames([(b'B', bind), (b'E', b'\0' + struct.pack('!I', 0)), (b'S', b'')])

    async def close(self):
        self.writer.close()
        await asyncio.wait_for(self.writer.wait_closed(), 5)


async def preflight(fixture, clients, directory):
    connected = []
    try:
        async def open_one():
            client = await AsyncPg.connect(fixture.proxy_port)
            connected.append(client)
        opened = await asyncio.wait_for(asyncio.gather(*(open_one() for _ in range(clients)), return_exceptions=True), 100)
        assert not [repr(result) for result in opened if isinstance(result, BaseException)], opened
        assert len(connected) == clients
        # Barrier: every frontend has Ready, no application SQL has been sent.
        count = await asyncio.to_thread(c.m.f.sql, fixture.nodes[0],
            "SELECT count(*) FROM pg_stat_activity WHERE backend_type='client backend' AND usename='postgres' AND pid<>pg_backend_pid()")
        idle = {'ready_frontends': clients, 'postgres_backends': int(count), 'application_sql_started': False}
        assert idle['postgres_backends'] <= POOL_SIZE
        dump(directory / 'idle-clients.json', idle)
        async def probe(client, index):
            try:
                assert await client.query('BEGIN') == []
                key = index % 10000 + 1
                assert await client.query(f'SELECT id,payload FROM comparison WHERE id={key}') == [[str(key), expected(key)]]
                sql = f'SELECT pg_backend_pid(), $1::integer + {index}'
                assert await client.prepare(b'pool_probe', sql) == []
                rows = await client.prepared(b'pool_probe', 7)
                assert len(rows) == 1 and rows[0][1] == str(index + 7)
                original = int(rows[0][0])
                assert client.status == b'T'
                assert await client.query('COMMIT') == []
                rows = await client.prepared(b'pool_probe', 11)
                assert len(rows) == 1 and rows[0][1] == str(index + 11)
                assert client.status == b'I'
                return {'client': index, 'transaction_pid': original, 'post_commit_pid': int(rows[0][0]),
                        'correct_prepared_results': True}
            finally:
                await client.close()
        rows = await asyncio.wait_for(asyncio.gather(*(probe(client, index) for index, client in enumerate(connected)), return_exceptions=True), 120)
        failures = [repr(row) for row in rows if isinstance(row, BaseException)]
        assert not failures, failures
        assert len(rows) == clients
        assert len({row['transaction_pid'] for row in rows if isinstance(row, dict)}) <= POOL_SIZE
        dump(directory / 'preflight.json', rows)
    finally:
        # Close even clients whose individual startup/probe failed. Errors stay
        # visible; cleanup never substitutes for a required completed operation.
        results = await asyncio.gather(*(client.close() for client in connected), return_exceptions=True)
        errors = [repr(result) for result in results if isinstance(result, BaseException)]
        dump(directory / 'preflight-cleanup.json', {'errors': errors})
        assert not errors, errors


def run_load(fixture, directory, clients, workload, measured):
    seconds = 10 if measured else 5
    argv = ['pgbench', '-h', '127.0.0.1', '-p', str(fixture.proxy_port), '-U', 'postgres', '-d', 'postgres',
            '-n', '--max-tries=1', '--exit-on-abort', '-M', 'simple', '-c', str(clients), '-j', str(min(16, clients)), '-T', str(seconds),
            '-f', str(directory.parent / 'transaction.sql')]
    if workload == 'churn':
        argv.append('-C')
    if measured:
        argv += ['-l', '--sampling-rate=1.0', '--log-prefix=' + str(directory / 'latency')]
    name = 'measurement' if measured else 'warmup'
    dump(directory / f'{name}-command.json', argv)
    started = time.monotonic()
    assert fixture.target_pid is not None
    before = process_info(fixture.target_pid)
    completed = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=seconds + 95)
    finished = time.monotonic()
    after = process_info(fixture.target_pid)
    text = completed.stdout + completed.stderr
    (directory / f'{name}.txt').write_text(text)
    assert completed.returncode == 0, text
    transactions = re.search(r'number of transactions actually processed:\s*(\d+)', completed.stdout)
    failures = re.search(r'number of failed transactions:\s*(\d+)', completed.stdout)
    assert transactions and failures and int(failures[1]) == 0 and int(transactions[1]) >= clients, text
    dump(directory / f'{name}-boundaries.json', {'before': before, 'after': after})
    # Includes setup and completion drain for every requested client. pgbench's
    # own reported TPS is also retained; do not drop slow queued completions.
    return {'transactions': int(transactions[1]), 'errors': int(failures[1]),
            'wall_seconds': finished - started, 'requested_seconds': seconds,
            'tps_including_connect_and_drain': int(transactions[1]) / (finished - started),
            'measurement_started_monotonic': started, 'measurement_finished_monotonic': finished,
            'cpu_seconds': after['cpu_seconds'] - before['cpu_seconds'],
            'cpu_seconds_per_transaction': (after['cpu_seconds'] - before['cpu_seconds']) / int(transactions[1])}


def latency_samples(directory, clients):
    by_client = {}
    samples = []
    for path in directory.glob('latency.*'):
        for line in path.read_text().splitlines():
            fields = line.split()
            assert len(fields) >= 6
            samples.append(float(fields[2]))
            by_client[int(fields[0])] = by_client.get(int(fields[0]), 0) + 1
    assert samples
    assert set(by_client)==set(range(clients)), by_client
    # Every measured client must complete; retain all queueing tails as well as
    # the separate exact 1,000-frontend prepared/row preflight.
    return {'sample_count': len(samples), 'clients_sampled': len(by_client),
            'sampling': '100%; every successful transaction retained',
            'client_completions_min': min(by_client.values()), 'client_completions_max': max(by_client.values()),
            'maximum_latency_ms': max(samples)/1000,
            'p50_ms': c.m.percentile(samples, .5), 'p95_ms': c.m.percentile(samples, .95),
            'p99_ms': c.m.percentile(samples, .99)}


def run_arm(fixture, output, target, clients, repetition, workload):
    directory = output / f'{workload}-c{clients}-r{repetition}-{target}'
    directory.mkdir()
    fixture.target_pid = None
    fixture.launch(target, directory)
    asyncio.run(preflight(fixture, clients, directory))
    with contextlib.closing(fixture.connect()) as client:
        assert client.query('SELECT payload FROM comparison WHERE id=42') == [[expected(42)]]
    stop, observations, errors = threading.Event(), [], []
    observer = threading.Thread(target=c.m.observe_connections, args=(fixture, stop, observations, errors))
    observer.start()
    try:
        run_load(fixture, directory, clients, workload, False)
        result = run_load(fixture, directory, clients, workload, True)
    finally:
        stop.set()
        observer.join(timeout=12)
        assert not observer.is_alive()
        dump(directory / 'connections.json', {'observations': observations, 'errors': errors})
    assert not errors, errors
    counts = [sum(row['backend_connections'].values()) for row in observations
              if result['measurement_started_monotonic'] <= row['monotonic'] <= result['measurement_finished_monotonic']]
    assert counts and 0 < max(counts) <= POOL_SIZE, counts
    result.update({'target': target, 'clients': clients, 'repetition': repetition, 'workload': workload,
                   'placement': fixture.mode, 'pool_size': POOL_SIZE,
                   'backend_connections_min': min(counts), 'backend_connections_peak': max(counts),
                   **latency_samples(directory, clients)})
    assert result['sample_count']==result['transactions'], result
    fixture.finish(directory, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--binary', type=Path, required=True)
    args = parser.parse_args()
    output, binary = args.output.resolve(), args.binary.resolve()
    output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(__file__,output/'harness.py')
    pg = shutil.which('pgbouncer')
    assert pg
    os.environ['PERF_PGBOUNCER_BIN'] = pg
    cpu = min(os.sched_getaffinity(0))
    dump(output / 'environment.json', {'command':sys.argv,'harness_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'pgbouncer_sha256':hashlib.sha256(Path(pg).read_bytes()).hexdigest(),'platform': platform.platform(), 'cpu_count': os.cpu_count(),
        'affinity': sorted(os.sched_getaffinity(0)), 'normalized_cpu': cpu, 'binary': str(binary),
        'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
        'pgbouncer': subprocess.check_output([pg, '--version'], text=True).strip(),
        'pgbench': subprocess.check_output(['pgbench', '--version'], text=True).strip(),
        'postgres_image': c.m.f.docker('image', 'inspect', c.m.f.IMAGE, '--format', '{{json .RepoDigests}}'),
        'pool_size': POOL_SIZE, 'client_capacity': CLIENT_CAPACITY, 'frontend_levels': [10, 100, 1000],
        'placements': ['unrestricted', 'normalized'], 'workloads': WORKLOADS, 'repetitions': 3,
        'warmup_seconds': 5, 'measurement_seconds': 10, 'metrics': 'disabled', 'tls': False,
        'auth': 'trust, owned loopback', 'wait_timeout_seconds': 60,
        'pgbench_worker_threads': 'min(16, clients); PostgreSQL distributes all 10/100/1000 clients across workers',
        'latency_logging': '100% for per-client completion evidence; shared logging overhead is included for all bounded-pool targets',
        'session_saturation': 'persistent clients may wait until active sessions close at requested deadline; all drain is included',
        'host_isolation': 'shared development host; only normalized proxy constrained to one CPU/one Tokio worker'})
    rows = []
    for placement in ('unrestricted', 'normalized'):
        path = output / placement
        path.mkdir()
        (path / 'transaction.sql').write_text(TRANSACTION_SCRIPT)
        fixture = PoolFixture(path, binary, mode=placement, cpu=cpu)
        try:
            fixture.start()
            for workload in WORKLOADS:
                for clients in (10, 100, 1000):
                    for repetition in (1, 2, 3):
                        offset = (repetition - 1 + int(clients >= 100)) % len(TARGETS)
                        targets = TARGETS[offset:] + TARGETS[:offset]
                        if repetition % 2 == 0:
                            targets = tuple(reversed(targets))
                        for target in targets:
                            row = run_arm(fixture, path, target, clients, repetition, workload)
                            rows.append(row)
                            dump(output / 'results.json', rows)
                            print(f'{len(rows)}/144 {placement}/{workload} {target} c={clients} '
                                  f'TPS={row["tps_including_connect_and_drain"]:.1f} errors={row["errors"]}', flush=True)
        finally:
            fixture.close()
    assert len(rows) == 144


if __name__ == '__main__':
    main()
