#!/usr/bin/env python3
"""Metrics overhead observations: same release binary, 18 full measured arms.

Run in locked dev shell after cargo build --release --locked:
  RUSTYBOUNCER_BIN=$PWD/target/release/rustybouncer python3 benchmarks/metrics.py
No time/concurrency reductions or performance thresholds are supported.
"""

import concurrent.futures
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
import failover as f  # noqa: E402
from metrics_support import MetricsCluster, request  # noqa: E402

ARMS = ('disabled', 'enabled', 'scraped')


def percentile(samples, quantile):
    ordered = sorted(samples)
    assert ordered, 'pgbench produced no latency samples'
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * quantile))] / 1000


def pgbench(cluster, clients, seconds, directory, measured):
    command = ['pgbench', '-h', '127.0.0.1', '-p', str(cluster.proxy_port), '-U', 'postgres',
               '-d', 'postgres', '-n', '-M', 'simple', '-c', str(clients), '-j', str(clients),
               '-T', str(seconds), '-f', str(directory.parent / 'select.sql')]
    if measured:
        command += ['-l', '--sampling-rate=0.1', '--log-prefix=' + str(directory / 'latency')]
    (directory / ('command.json' if measured else 'warmup-command.json')).write_text(json.dumps(command))
    started = time.monotonic()
    log = directory / ('pgbench.txt' if measured else 'warmup.txt')
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=seconds + 30)
    except subprocess.TimeoutExpired as exc:
        # Python may return bytes on timeout even when text=True was requested.
        chunks = [part.decode(errors='replace') if isinstance(part, bytes) else part or ''
                  for part in (exc.stdout, exc.stderr)]
        log.write_text(''.join(chunks) + '\npgbench exceeded its operation deadline\n')
        raise
    elapsed = time.monotonic() - started
    log.write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    # Missing/error output must fail, never turn into a zero-error observation.
    processed = re.search(r'number of transactions actually processed:\s*(\d+)', result.stdout)
    failed = re.search(r'number of failed transactions:\s*(\d+)', result.stdout)
    tps = re.search(r'tps = ([\d.]+)', result.stdout)
    assert processed and failed and tps, result.stdout
    assert int(failed[1]) == 0, result.stdout
    return {'wall_seconds': elapsed, 'transactions': int(processed[1]), 'errors': int(failed[1]),
            'tps': float(tps[1]), 'measurement_started_monotonic': started,
            'measurement_finished_monotonic': started + elapsed}


def scrape_loop(port, stop, observations, failures):
    deadline = time.monotonic()
    while not stop.is_set():
        try:
            status, content, body = request(port)
            assert status == 200
            observations.append({'monotonic': time.monotonic(), 'content_type': content, 'body': body})
        except Exception as exc:
            failures.append(repr(exc))
        deadline += 1
        stop.wait(max(0, deadline - time.monotonic()))


def preflight(cluster, clients):
    connected, lock = [], threading.Lock()

    def connect(_):
        client = cluster.connect()
        with lock:
            connected.append(client)
        return client

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=clients) as executor:
            # The executor joins even when one connect fails; all successful
            # connections are owned before any future can expose that failure.
            list(executor.map(connect, range(clients)))
            responses = list(executor.map(lambda client: client.query('SELECT 1'), connected))
            assert responses == [[['1']]] * clients
    finally:
        for client in connected:
            client.close()


def observe_connections(cluster, stop, observations, failures):
    query = ("SELECT count(*) FROM pg_stat_activity WHERE backend_type='client backend' "
             "AND usename='postgres' AND pid<>pg_backend_pid()")
    while not stop.is_set():
        began, counts = time.monotonic(), {}
        try:
            for name, node in zip(cluster.names, cluster.nodes):
                result = subprocess.run(['docker', 'exec', node, 'psql', '-h', '127.0.0.1',
                                         '-U', 'postgres', '-At', '-c', query],
                                        text=True, capture_output=True, check=True, timeout=3)
                counts[name] = int(result.stdout.strip())
            observations.append({'monotonic': began, 'backend_connections': counts})
        except Exception as exc:
            failures.append(repr(exc))
        stop.wait(max(0, 5 - (time.monotonic() - began)))


def run_arm(cluster, output, clients, repetition, arm):
    directory = output / f'c{clients}-r{repetition}-{arm}'
    directory.mkdir()
    cluster.restart(enabled=arm != 'disabled')
    # Verify all required clients can stay connected simultaneously before timing.
    preflight(cluster, clients)
    stop, observations, failures = threading.Event(), [], []
    connections, connection_errors = [], []
    observer = threading.Thread(target=observe_connections,
                                args=(cluster, stop, connections, connection_errors))
    observer.start()
    worker = None
    if arm == 'scraped':
        worker = threading.Thread(target=scrape_loop, args=(cluster.metrics_port, stop, observations, failures))
        worker.start()
    try:
        pgbench(cluster, clients, 5, directory, False)
        result = pgbench(cluster, clients, 30, directory, True)
    finally:
        stop.set()
        if worker:
            worker.join(timeout=5)
            assert not worker.is_alive()
        observer.join(timeout=12)
        assert not observer.is_alive()
        (directory / 'scrapes.json').write_text(json.dumps({'observations': observations, 'errors': failures}, indent=2))
        (directory / 'connections.json').write_text(json.dumps({'observations': connections, 'errors': connection_errors}, indent=2))
    assert not failures, failures
    assert not connection_errors, connection_errors
    measured_counts = [sum(sample['backend_connections'].values()) for sample in connections
                       if result['measurement_started_monotonic'] <= sample['monotonic'] <= result['measurement_finished_monotonic']]
    assert measured_counts and max(measured_counts) >= clients, measured_counts
    samples = []
    for path in directory.glob('latency.*'):
        for line in path.read_text().splitlines():
            columns = line.split()
            assert len(columns) >= 6, line
            samples.append(float(columns[2]))  # pgbench per-transaction latency, microseconds
    result.update({'clients': clients, 'repetition': repetition, 'arm': arm, 'sample_count': len(samples),
                   'sample_policy': 'pgbench independent random 10% transaction logging; all transaction errors retained',
                   'p50_ms': percentile(samples, .5), 'p95_ms': percentile(samples, .95),
                   'p99_ms': percentile(samples, .99), 'scrapes': len(observations),
                   'backend_connection_samples': len(measured_counts),
                   'backend_connections_min': min(measured_counts),
                   'backend_connections_max': max(measured_counts)})
    (directory / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def main():
    output = Path(os.environ.get('METRICS_BENCH_EVIDENCE', 'metrics-benchmark-evidence')).resolve()
    output.mkdir(parents=True, exist_ok=False)
    binary = Path(os.environ.get('RUSTYBOUNCER_BIN', f.ROOT / 'target/release/rustybouncer')).resolve()
    f.BINARY = binary
    environment = {'binary': str(binary), 'sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
                   'platform': platform.platform(), 'cpu_count': os.cpu_count(),
                   'warmup_seconds': 5, 'measure_seconds': 30, 'repetitions': 3,
                   'postgres_max_connections': 400, 'frontend_capacity': 256}
    for tool in ('pgbench', 'docker'):
        environment[tool] = subprocess.check_output([tool, '--version'], text=True).strip()
    (output / 'environment.json').write_text(json.dumps(environment, indent=2) + '\n')
    (output / 'select.sql').write_text('SELECT 1;\n')
    cluster = MetricsCluster(output / 'fixture')
    results = []
    try:
        cluster.start()
        for clients in (10, 100):
            for repetition in range(1, 4):
                order = ARMS if repetition % 2 else tuple(reversed(ARMS))
                for arm in order:
                    results.append(run_arm(cluster, output, clients, repetition, arm))
                    (output / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
    finally:
        cluster.close()
    assert len(results) == 18
    print(json.dumps(results, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
