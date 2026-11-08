#!/usr/bin/env python3
"""PERF-005/009 additional CTE/large/mixed workloads; original matrix stays mandatory.

All 180 arms: five targets, two CPU placements, 10/100 clients, three repetitions,
three workloads. Each has 5s warmup and 10s observation (not the original 30s).
"""
import argparse
import contextlib
import hashlib
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks import compare as c  # noqa: E402
from benchmarks.performance_profile import Instrumented, dump, process_info  # noqa: E402

WORKLOADS = ('cte', 'large', 'mixed')
SCRIPTS = {
    'cte': '\\set id random(1, 10000)\nWITH chosen AS (SELECT payload FROM comparison WHERE id = :id) SELECT payload FROM chosen;\n',
    'large': 'SELECT id, payload FROM comparison WHERE id <= 2048 ORDER BY id;\n',
    'short': '\\set id random(1, 10000)\nSELECT payload FROM comparison WHERE id = :id;\n',
    'long': 'SELECT pg_sleep(0.02);\n',
}


class Observed(Instrumented):
    def command(self, command, directory):
        return command if self.mode == 'unrestricted' else super().command(command, directory)


def expected(key):
    return hashlib.md5(str(key).encode(), usedforsecurity=False).hexdigest() * 4


def oracle(fixture, workload):
    with contextlib.closing(fixture.connect()) as client:
        if workload == 'cte':
            assert client.query('WITH chosen AS (SELECT payload FROM comparison WHERE id = 42) SELECT payload FROM chosen') == [[expected(42)]]
        elif workload == 'large':
            assert client.query(SCRIPTS['large']) == [[str(key), expected(key)] for key in range(1, 2049)]
        else:
            assert client.query('SELECT payload FROM comparison WHERE id = 42') == [[expected(42)]]
            assert client.query(SCRIPTS['long']) == [['']]


def command(fixture, clients, directory, workload, measured):
    seconds = 10 if measured else 5
    argv = ['pgbench', '-h', '127.0.0.1', '-p', str(fixture.proxy_port), '-U', 'postgres',
            '-d', 'postgres', '-n', '-M', 'simple', '-c', str(clients), '-j', str(clients),
            '-T', str(seconds)]
    scripts = [('short', 9), ('long', 1)] if workload == 'mixed' else [(workload, 1)]
    for name, weight in scripts:
        argv += ['-f', str(directory.parent / f'{name}.sql') + f'@{weight}']
    if measured:
        argv += ['-l', '--sampling-rate=0.1', '--log-prefix=' + str(directory / 'latency')]
    return argv


def load(fixture, clients, directory, workload, measured):
    argv = command(fixture, clients, directory, workload, measured)
    name = 'measurement' if measured else 'warmup'
    dump(directory / f'{name}-command.json', argv)
    started = time.monotonic()
    before = process_info(fixture.target_pid) if fixture.target_pid else None
    try:
        output = subprocess.run(argv, capture_output=True, text=True, timeout=40, check=False)
    except subprocess.TimeoutExpired as error:
        parts = [x.decode(errors='replace') if isinstance(x, bytes) else x or '' for x in (error.stdout,error.stderr)]
        (directory / f'{name}.txt').write_text(''.join(parts))
        raise
    ended = time.monotonic()
    after = process_info(fixture.target_pid) if fixture.target_pid else None
    (directory / f'{name}.txt').write_text(output.stdout + output.stderr)
    assert output.returncode == 0, output.stdout + output.stderr
    count = re.search(r'number of transactions actually processed:\s*(\d+)', output.stdout)
    errors = re.search(r'number of failed transactions:\s*(\d+)', output.stdout)
    tps = re.search(r'tps = ([\d.]+)', output.stdout)
    assert count and errors and tps and int(count[1]) > 0 and int(errors[1]) == 0
    result = {'measurement_started_monotonic': started, 'measurement_finished_monotonic': ended,
              'wall_seconds': ended-started, 'transactions': int(count[1]), 'errors': int(errors[1]),
              'tps': float(tps[1])}
    if before is not None and after is not None:
        dump(directory / f'{name}-boundaries.json', {'before': before, 'after': after})
        result['cpu_seconds_per_q_including_connect'] = (after['cpu_seconds']-before['cpu_seconds']) / result['transactions']
    return result


def quantiles(samples):
    assert samples
    return {'samples': len(samples), **{f'p{int(q*100)}_ms': c.m.percentile(samples,q) for q in (.5,.95,.99)}}


def latencies(directory, workload):
    groups = {}
    for path in directory.glob('latency.*'):
        for line in path.read_text().splitlines():
            columns = line.split()
            assert len(columns) >= 6
            groups.setdefault(int(columns[3]), []).append(float(columns[2]))
    expected_scripts = {0,1} if workload == 'mixed' else {0}
    assert set(groups) == expected_scripts, 'missing workload latency samples'
    return {'all': quantiles([value for group in groups.values() for value in group]),
            'scripts': {str(key): quantiles(values) for key,values in groups.items()}}


def arm(fixture, output, clients, repetition, target, workload):
    directory = output / f'c{clients}-r{repetition}-{target}'
    directory.mkdir()
    fixture.target_pid = None
    fixture.launch(target, directory)
    c.m.preflight(fixture, clients)
    oracle(fixture, workload)
    stop, observations, failures = threading.Event(), [], []
    observer = threading.Thread(target=c.m.observe_connections, args=(fixture,stop,observations,failures))
    observer.start()
    try:
        load(fixture,clients,directory,workload,False)
        result = load(fixture,clients,directory,workload,True)
    finally:
        stop.set()
        observer.join(timeout=12)
        assert not observer.is_alive()
        dump(directory / 'connections.json', {'observations': observations, 'errors': failures})
    assert not failures, failures
    counts = [sum(row['backend_connections'].values()) for row in observations
              if result['measurement_started_monotonic'] <= row['monotonic'] <= result['measurement_finished_monotonic']]
    assert counts and max(counts)>0
    result.update({'clients': clients, 'repetition': repetition, 'target': target,
                   'workload': workload, 'placement': fixture.mode, 'latencies': latencies(directory, workload),
                   'backend_connections_min': min(counts), 'backend_connections_peak': max(counts),
                   'correctness': 'complete concurrent preflight plus workload-specific exact row oracle before load; pgbench checks SQL/protocol errors for every transaction'})
    oracle(fixture, workload)
    fixture.finish(directory, result)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--binary',type=Path,required=True)
    args=parser.parse_args()
    output=args.output.resolve()
    output.mkdir(parents=True,exist_ok=False)
    reference=shutil.which('pgbouncer')
    assert reference
    os.environ['PERF_PGBOUNCER_BIN']=reference
    binary=args.binary.resolve()
    shutil.copyfile(__file__,output/'harness.py')
    dump(output/'environment.json', {'binary': str(binary), 'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
         'command': sys.argv, 'harness_affinity': sorted(os.sched_getaffinity(0)), 'load_start': os.getloadavg(),
         'arm_count': 180, 'warmup_seconds': 5, 'observation_seconds': 10, 'repetitions': 3,
         'mixed_weights': {'short':9,'long_20ms':1}, 'large_rows':2048, 'payload_bytes':128,
         'metrics':False,'tls':False,'authentication':'trust in owned loopback fixture',
         'limitation':'shared host; 10s additional observations are not the original 30s comparison or production SLO evidence'})
    results=[]
    for placement in ('unrestricted','normalized'):
        for workload in WORKLOADS:
            directory=output/placement/workload
            directory.mkdir(parents=True)
            for name,sql in SCRIPTS.items():
                (directory/f'{name}.sql').write_text(sql)
            fixture=Observed(directory,binary,mode=placement,cpu=min(os.sched_getaffinity(0)))
            try:
                fixture.start()
                for clients in (10,100):
                    for repetition in (1,2,3):
                        for target in c.order(repetition,clients):
                            results.append(arm(fixture,directory,clients,repetition,target,workload))
                            dump(output/'results.json',results)
                            print(f'{len(results)}/180 {placement} {workload} {clients} {target}',flush=True)
            finally:
                fixture.close()
    assert len(results)==180


if __name__=='__main__':
    main()
