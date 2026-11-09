#!/usr/bin/env python3
"""PERF-009: 24 real crash/promotion load experiments; no SQL operation replay.

Four shared poolers x 10/100 actual workers x three repetitions. This is a fault
experiment with an explicit PgBouncer reconfiguration adapter, not normal TPS.
Synchronous replication to both standbys protects acknowledged pre-crash writes.
"""
import argparse
import asyncio
import contextlib
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
import failover as f  # noqa: E402
import protocol_support as p  # noqa: E402
from metrics_support import MetricsCluster  # noqa: E402
from pool_support import Client  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.performance_profile import process_info  # noqa: E402

TARGETS = ('pgbouncer-session', 'pgbouncer-transaction', 'rustybouncer-pool-session', 'rustybouncer-pool-transaction')


def dump(path, data):
    path.write_text(json.dumps(data, indent=2) + '\n')


class Cluster(MetricsCluster):
    def __init__(self, output, binary, target):
        super().__init__(output)
        self.binary, self.target = binary, target
        self.configuration = output / 'pooler.conf'
        self.events = []

    def configure(self, node=0):
        mode = 'transaction' if self.target.endswith('transaction') else 'session'
        if self.target.startswith('pgbouncer'):
            users = self.evidence / 'users.txt'
            users.write_text('"postgres" ""\n')
            text = f'''[databases]
postgres = host=127.0.0.1 port={f.port(self.nodes[node])} dbname=postgres
[pgbouncer]
listen_addr = 127.0.0.1
listen_port = {self.proxy_port}
unix_socket_dir =
auth_type = trust
auth_file = {users}
admin_users = postgres
pool_mode = {mode}
default_pool_size = 16
max_db_connections = 16
max_user_connections = 16
max_client_conn = 1200
max_prepared_statements = 16
query_wait_timeout = 10
query_timeout = 5
server_connect_timeout = 2
server_login_retry = 1
log_connections = 0
log_disconnections = 0
'''
        else:
            text = f'''listen = "127.0.0.1:{self.proxy_port}"
metrics_listen = "127.0.0.1:{self.metrics_port}"
route_selects = true
pool_mode = "{mode}"
pool_size = 16
max_pool_backends = 16
max_clients = 1200
probe_interval_ms = 100
probe_timeout_ms = 300
wait_timeout_ms = 10000
startup_timeout_ms = 5000
query_timeout_ms = 5000
probe_user = "rustybouncer_metrics_probe"
probe_database = "postgres"
'''
            text += ''.join(f'[[nodes]]\nname = "{index}"\nhost = "127.0.0.1"\nport = {f.port(name)}\n'
                            for index, name in enumerate(self.nodes))
        self.configuration.write_text(text)
        (self.evidence / f'configuration-node-{node}.txt').write_text(text)

    def begin(self):
        self.start()
        self.stop_proxy()
        # Both standbys must durably acknowledge every primary commit. Without
        # this, async WAL loss would be a database policy, not a pooler failure.
        f.eventually(lambda: f.sql(self.nodes[0], "SELECT count(*) FROM pg_stat_replication WHERE state='streaming'") == '2')
        f.sql(self.nodes[0], "ALTER SYSTEM SET synchronous_standby_names='ANY 2 (*)'")
        f.sql(self.nodes[0], 'SELECT pg_reload_conf()')
        self.configure()
        if self.target.startswith('pgbouncer'):
            executable=shutil.which('pgbouncer')
            assert executable
            argv=[executable,str(self.configuration)]
        else:
            argv=[str(self.binary),str(self.configuration)]
        self.process = subprocess.Popen(argv, stdout=self.log, stderr=self.log)
        dump(self.evidence / 'process-command.json', argv)
        f.eventually(self.ready)

    def fault(self):
        self.events.append({'event': 'kill-primary-start', 'monotonic': time.monotonic()})
        f.docker('kill', self.nodes[0])
        self.events.append({'event': 'primary-killed', 'monotonic': time.monotonic()})
        self.promote(1)
        # Explicit cluster-manager action for all targets; promoted replicas did
        # not inherit the old primary's post-basebackup ALTER SYSTEM setting.
        assert f.sql(self.nodes[1], 'SHOW synchronous_standby_names') == ''
        self.events.append({'event': 'replica-promoted', 'monotonic': time.monotonic()})
        if self.target.startswith('pgbouncer'):
            self.configure(1)
            with contextlib.closing(Client(self.proxy_port, database='pgbouncer')) as admin:
                admin.query('RELOAD')
                admin.query('RECONNECT postgres')
            self.events.append({'event': 'external-pgbouncer-reload-reconnect', 'monotonic': time.monotonic()})
        dump(self.evidence / 'fault-events.json', self.events)
        return time.monotonic()

    def close(self):
        self.stop_proxy()
        if self.log_path.exists():
            shutil.copyfile(self.log_path, self.evidence / 'pooler.log')
        for index, node in enumerate(self.nodes):
            with contextlib.suppress(RuntimeError):
                (self.evidence / f'postgres-{index}.log').write_text(f.docker('logs', '--tail', '120', node))
        super().close()


class Wire(p.AsyncClient):
    async def query(self, sql):
        self.writer.write(p.frame(b'Q', sql.encode() + b'\0'))
        await self.writer.drain()
        return p.rows(await self.receive())


async def wait_until(predicate, timeout, message):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, message
        await asyncio.sleep(.02)


async def observe(cluster, stop, rows):
    def sample():
        assert cluster.process is not None and cluster.process.poll() is None
        row={'monotonic':time.monotonic(),'proxy':process_info(cluster.process.pid),'nodes':{}}
        query=("SELECT count(*) FROM pg_stat_activity WHERE backend_type='client backend' "
               "AND usename='postgres' AND pid<>pg_backend_pid()")
        for index,node in enumerate(cluster.nodes):
            try:
                row['nodes'][str(index)]={'connections':int(f.sql(node,query))}
            except RuntimeError:
                # A killed database is an explicit unavailable observation, not
                # a fictional zero connection count or a hidden sampler error.
                row['nodes'][str(index)]={'unavailable':True}
        return row
    while not stop.is_set():
        rows.append(await asyncio.to_thread(sample))
        try:
            await asyncio.wait_for(stop.wait(),1)
        except asyncio.TimeoutError:
            pass


async def load(cluster, clients):
    stop = asyncio.Event()
    barrier = asyncio.Event()
    first, recovered, events, cleanup = set(), set(), [], []
    observed=[]
    observer=asyncio.create_task(observe(cluster,stop,observed))
    fault_at, promoted_at = None, None

    async def worker(index):
        sequence = 0
        await barrier.wait()
        while not stop.is_set():
            sequence += 1
            assert sequence < 10000000
            key = index * 10000000 + sequence
            token = f'worker-{index}-operation-{sequence}'
            write = sequence % 2 == 0
            event = {'worker': index, 'sequence': sequence, 'write': write, 'id': key,
                     'token': token, 'started': time.monotonic()}
            client = None
            try:
                client = await asyncio.wait_for(Wire.connect(cluster.proxy_port), 12)
                query = (f"INSERT INTO continuity VALUES({key},'{token}') RETURNING id,payload" if write
                         else f"SELECT {key},'{token}'")
                rows = await asyncio.wait_for(client.query(query), 12)
                assert rows == [[str(key).encode(), token.encode()]], rows
                event['outcome'] = 'ok'
                if write:
                    first.add(index)
                if write and promoted_at is not None and event['started'] >= promoted_at:
                    recovered.add(index)
            except (f.PgError, OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as error:
                event.update({'outcome': 'connection_or_query_error', 'code': getattr(error, 'code', ''),
                              'error': type(error).__name__})
            finally:
                event['finished'] = time.monotonic()
                events.append(event)
                if client is not None:
                    try:
                        await client.close()
                    except OSError as error:
                        cleanup.append({'worker': index, 'time': time.monotonic(), 'error': type(error).__name__})

    workers = [asyncio.create_task(worker(index)) for index in range(clients)]
    try:
        barrier.set()
        await wait_until(lambda: len(first) == clients, 15, 'all requested workers must acknowledge a write before fault')
        await asyncio.sleep(5)
        fault_at = time.monotonic()
        promoted_at = await asyncio.to_thread(cluster.fault)
        await wait_until(lambda: len(recovered) == clients, 15, 'every worker must acknowledge a new write after promotion')
        stable_at = time.monotonic()
        await asyncio.sleep(5)
    finally:
        stop.set()
        try:
            results = await asyncio.wait_for(asyncio.gather(*workers, return_exceptions=True), 20)
        finally:
            try:
                await asyncio.wait_for(observer,35)
            finally:
                dump(cluster.evidence / 'observations.json', observed)
                dump(cluster.evidence / 'operations.json', events)
                dump(cluster.evidence / 'connection-cleanup.json', cleanup)
        failures = [repr(result) for result in results if isinstance(result, BaseException)]
        assert not failures, failures
    return await validate(cluster,events,cleanup,(fault_at,promoted_at,stable_at),(first,recovered,clients))


async def validate(cluster,events,cleanup,times,workers):
    fault_at,promoted_at,stable_at=times
    first,recovered,clients=workers
    assert fault_at is not None and promoted_at is not None
    assert len(first) == len(recovered) == clients
    unexpected = [event for event in events if event['outcome'] != 'ok'
                  and (event['finished'] < fault_at or event['started'] >= stable_at)]
    assert not unexpected, unexpected
    unexpected_cleanup=[event for event in cleanup if event['time']<fault_at or event['time']>=stable_at]
    assert not unexpected_cleanup,unexpected_cleanup
    stable = [event for event in events if event['started'] >= stable_at]
    assert {event['worker'] for event in stable if event['outcome'] == 'ok' and event['write']} == set(range(clients))
    oracle=await database_oracle(cluster,events)
    return {'clients': clients, 'target': cluster.target, 'operations': len(events), 'fault_at': fault_at,
            'promoted_at': promoted_at, 'all_workers_recovered_at': stable_at,
            'all_workers_recovery_seconds': stable_at - fault_at, **oracle,
            'expected_fault_operation_errors': sum(event['outcome'] != 'ok' for event in events),
            'unexpected_operation_errors': len(unexpected), 'replayed_operations': 0,
            'startup_connection_per_operation': True, 'post_recovery_observation_seconds': 5,
            'stable_operations': len(stable), 'active_workers': len(first), 'recovered_workers': len(recovered)}


async def database_oracle(cluster,events):
    committed = await asyncio.to_thread(f.sql, cluster.nodes[1], 'SELECT id,payload FROM continuity ORDER BY id')
    raw_rows=[line.split('|',1) for line in committed.splitlines()]
    actual=dict(raw_rows)
    assert len(raw_rows)==len(actual), 'duplicate committed identities'
    acknowledged = {str(event['id']): event['token'] for event in events if event['write'] and event['outcome'] == 'ok'}
    submitted = {str(event['id']): event['token'] for event in events if event['write']}
    assert all(actual.get(key) == value for key, value in acknowledged.items()), 'acknowledged synchronous write lost or corrupted'
    assert all(submitted.get(key) == value for key, value in actual.items()), 'unsubmitted/cross-client write found'
    assert len(actual) == len(set(actual))
    dump(cluster.evidence / 'database-oracle.json', {'committed': actual, 'acknowledged': acknowledged,
        'uncertain_committed_ids': sorted(set(actual) - set(acknowledged)), 'submitted_write_count': len(submitted)})
    return {'acknowledged_writes':len(acknowledged),'actual_committed_writes':len(actual),
            'uncertain_committed_writes':len(set(actual)-set(acknowledged))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--binary', type=Path, required=True)
    args = parser.parse_args()
    output, binary = args.output.resolve(), args.binary.resolve()
    output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(__file__,output/'harness.py')
    f.BINARY = binary
    dump(output / 'environment.json', {'binary': str(binary), 'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
        'command': sys.argv, 'targets': TARGETS, 'clients': [10,100], 'repetitions': 3, 'arms': 24,
        'postgres_image': f.docker('image', 'inspect', f.IMAGE, '--format', '{{json .RepoDigests}}'),
        'replication': 'ANY 2 (*) synchronous acknowledgements before primary crash',
        'reference_adapter': 'after real promotion, rewrite PgBouncer backend and issue RELOAD + RECONNECT postgres',
        'rust_adapter': 'no pooler restart, reload or endpoint rewrite; topology discovery',
        'placement': 'unrestricted shared host; functional fault experiment, not normal throughput comparison',
        'scope': 'one new connection per unique operation; no application SQL replay, all workers must recover'})
    results = []
    for clients in (10,100):
        for repetition in (1,2,3):
            targets = TARGETS[repetition-1:] + TARGETS[:repetition-1]
            for target in targets:
                directory = output / f'c{clients}-r{repetition}-{target}'
                cluster = Cluster(directory, binary, target)
                try:
                    cluster.begin()
                    result = asyncio.run(load(cluster,clients))
                    result['repetition'] = repetition
                    results.append(result)
                    dump(directory / 'result.json', result)
                    dump(output / 'results.json', results)
                    print(f'{len(results)}/24 {target} c={clients} recovery={result["all_workers_recovery_seconds"]:.3f}s', flush=True)
                finally:
                    cluster.close()
    assert len(results) == 24


if __name__ == '__main__':
    main()
