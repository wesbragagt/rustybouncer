#!/usr/bin/env python3
"""S10/S12: matched-primary comparison; 30 mandatory arms, no performance budget.

Run: nix develop --command python3 benchmarks/compare.py --output NEW_DIRECTORY
Requires a release Rust binary, Docker, PgBouncer and pgbench. All fixtures owned.
"""
import argparse
import contextlib
import hashlib
import json
import os
import platform
import subprocess
import sys
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from benchmarks import metrics as m  # noqa: E402

TARGETS = ('direct', 'pgbouncer-session', 'pgbouncer-transaction', 'rustybouncer-session', 'rustybouncer-routed')


def order(repetition, clients):
    offset = (repetition - 1 + int(clients == 100)) % len(TARGETS)
    rotated = TARGETS[offset:] + TARGETS[:offset]
    return tuple(reversed(rotated)) if repetition % 2 == 0 else rotated


def stop(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


class Fixture:
    def __init__(self, output, binary):
        self.output, self.binary = output, binary
        self.nodes = ['rustybouncer-compare-' + uuid.uuid4().hex[:12]]
        self.names = ('primary',)
        self.process = None
        self.log = None
        self.proxy_port = 0
        self.pg_port = 0

    def start(self):
        m.f.docker('run', '-d', '--name', self.nodes[0], '-p', '127.0.0.1::5432',
                   '-e', 'POSTGRES_HOST_AUTH_METHOD=trust', m.f.IMAGE,
                   '-c', 'max_connections=400')
        self.pg_port = m.f.port(self.nodes[0])
        m.f.eventually(lambda: m.f.sql(self.nodes[0], 'SELECT 1') == '1')
        m.f.sql(self.nodes[0], 'CREATE ROLE comparison_probe LOGIN; '
                'CREATE TABLE comparison AS SELECT n AS id, repeat(md5(n::text),4) AS payload '
                'FROM generate_series(1,10000) n; ALTER TABLE comparison ADD PRIMARY KEY(id); ANALYZE comparison')
        m.f.sql(self.nodes[0], 'SELECT count(*) FROM comparison')

    def connect(self):
        return m.f.Pg(self.proxy_port)

    def launch(self, target, directory):
        stop(self.process)
        if self.log is not None:
            self.log.close()
        self.process = None
        if target == 'direct':
            self.proxy_port = self.pg_port
            return
        self.proxy_port = self.available_port()
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
pool_mode = {target.split('-')[1]}
default_pool_size = 256
max_client_conn = 256
log_connections = 0
log_disconnections = 0
''')
            command = ['pgbouncer', str(path)]
        else:
            path = directory / 'router.toml'
            path.write_text(f'''listen = "127.0.0.1:{self.proxy_port}"
max_clients = 256
route_selects = {str(target.endswith('routed')).lower()}
probe_interval_ms = 500
probe_timeout_ms = 1000
wait_timeout_ms = 10000
startup_timeout_ms = 5000
probe_user = "comparison_probe"
probe_database = "postgres"
[[nodes]]
name = "primary"
host = "127.0.0.1"
port = {self.pg_port}
''')
            command = [str(self.binary), str(path)]
        self.log = (directory / 'process.log').open('w')
        self.process = subprocess.Popen(command, stdout=self.log, stderr=subprocess.STDOUT)
        (directory / 'process-command.json').write_text(json.dumps(command))
        process = self.process
        def ready():
            if process.poll() is not None:
                raise AssertionError(f'{target} exited; inspect {directory / "process.log"}')
            with contextlib.closing(self.connect()) as client:
                return client.query('SELECT 1') == [['1']]
        m.f.eventually(ready)

    @staticmethod
    def available_port():
        from metrics_support import free_port
        return free_port()

    def close(self):
        try:
            stop(self.process)
        finally:
            if self.log:
                self.log.close()
            with contextlib.suppress(RuntimeError):
                (self.output / 'postgres.log').write_text(m.f.docker('logs', self.nodes[0]))
            m.f.docker('rm', '-fv', self.nodes[0])


def run_arm(fixture, output, clients, repetition, target):
    directory = output / f'c{clients}-r{repetition}-{target}'
    directory.mkdir()
    fixture.launch(target, directory)
    m.preflight(fixture, clients)
    with contextlib.closing(fixture.connect()) as client:
        for key in (1, 10000):
            expected = hashlib.md5(str(key).encode(), usedforsecurity=False).hexdigest() * 4
            assert client.query(f'SELECT payload FROM comparison WHERE id = {key}') == [[expected]]
    stop_event, observations, errors = threading.Event(), [], []
    observer = threading.Thread(target=m.observe_connections, args=(fixture, stop_event, observations, errors))
    observer.start()
    try:
        m.pgbench(fixture, clients, 5, directory, False)
        result = m.pgbench(fixture, clients, 30, directory, True)
    finally:
        stop_event.set()
        observer.join(timeout=12)
        assert not observer.is_alive()
        (directory / 'connections.json').write_text(json.dumps({'observations': observations, 'errors': errors}, indent=2))
    assert not errors, errors
    samples = []
    for path in directory.glob('latency.*'):
        for line in path.read_text().splitlines():
            fields = line.split()
            assert len(fields) >= 6
            samples.append(float(fields[2]))
    counts = [sum(row['backend_connections'].values()) for row in observations
              if result['measurement_started_monotonic'] <= row['monotonic'] <= result['measurement_finished_monotonic']]
    assert counts and max(counts) > 0
    result.update({'target': target, 'clients': clients, 'repetition': repetition,
                   'sample_count': len(samples), 'p50_ms': m.percentile(samples,.5),
                   'p95_ms': m.percentile(samples,.95), 'p99_ms': m.percentile(samples,.99),
                   'backend_connections_min': min(counts), 'backend_connections_peak': max(counts)})
    (directory / 'result.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--binary',type=Path,default=ROOT/'target/release/rustybouncer')
    args = parser.parse_args()
    output, binary = args.output.resolve(), args.binary.resolve()
    output.mkdir(parents=True,exist_ok=False)
    environment = {'platform':platform.platform(), 'cpu_count':os.cpu_count(),
                   'load_average_start':os.getloadavg(), 'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),
                   'pgbouncer':subprocess.check_output(['pgbouncer','--version'],text=True).strip(),
                   'pgbench':subprocess.check_output(['pgbench','--version'],text=True).strip(),
                   'postgres_image':m.f.docker('image','inspect',m.f.IMAGE,'--format','{{json .RepoDigests}}'),
                   'warmup_seconds':5,'measure_seconds':30,'repetitions':3,'sampling_rate':.1,
                   'max_clients':256,'pgbouncer_pool_size':256,'postgres_max_connections':400,
                   'tls':False,'authentication':'trust, owned loopback fixture','metrics':'disabled',
                   'workload':'random indexed lookup, 10000 rows, 128-byte payload; persistent simple protocol',
                   'host_isolation':'shared development host, no exclusive CPU isolation'}
    (output/'environment.json').write_text(json.dumps(environment,indent=2)+'\n')
    (output/'select.sql').write_text('\\set id random(1, 10000)\nSELECT payload FROM comparison WHERE id = :id;\n')
    fixture = Fixture(output,binary)
    results = []
    try:
        fixture.start()
        for clients in (10,100):
            for repetition in (1,2,3):
                for target in order(repetition,clients):
                    result = run_arm(fixture,output,clients,repetition,target)
                    results.append(result)
                    (output/'results.json').write_text(json.dumps(results,indent=2)+'\n')
                    print(f'{len(results)}/30 {target} c={clients} TPS={result["tps"]:.0f} errors={result["errors"]}',flush=True)
    finally:
        fixture.close()
    assert len(results)==30


if __name__=='__main__':
    main()
