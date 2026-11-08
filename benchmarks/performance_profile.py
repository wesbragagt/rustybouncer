#!/usr/bin/env python3
"""PERF-001: 30 equal-core timing arms, followed by separate attribution runs.

Run the unchanged compare.py matrix separately. No required arm is optional.
"""
import argparse
import contextlib
import hashlib
import json
import os
import platform
import re
import resource
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks import compare as c  # noqa: E402

REAL_POPEN = subprocess.Popen


def dump(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def children(pid):
    pending, result = [pid], []
    while pending:
        current = pending.pop()
        result.append(current)
        try:
            pending.extend(map(int, Path(f'/proc/{current}/task/{current}/children').read_text().split()))
        except FileNotFoundError:
            pass
    return result


def process_info(pid):
    base = Path(f'/proc/{pid}')
    stat = (base / 'stat').read_text().rsplit(')', 1)[1].split()
    threads = []
    for directory in sorted((base / 'task').iterdir()):
        try:
            tid = int(directory.name)
            status = (directory / 'status').read_text()
            switches = {line.split(':')[0]: int(line.split(':')[1]) for line in status.splitlines()
                        if line.startswith(('voluntary_ctxt_switches:', 'nonvoluntary_ctxt_switches:'))}
            threads.append({'tid': tid, 'name': (directory / 'comm').read_text().strip(),
                            'affinity': sorted(os.sched_getaffinity(tid)), **switches})
        except (ProcessLookupError, FileNotFoundError):
            pass
    ticks = os.sysconf('SC_CLK_TCK')
    return {'monotonic': time.monotonic(), 'pid': pid, 'cpu_seconds': (int(stat[11])+int(stat[12]))/ticks,
            'user_cpu_seconds': int(stat[11])/ticks, 'system_cpu_seconds': int(stat[12])/ticks,
            'rss_bytes': int(stat[21])*os.sysconf('SC_PAGE_SIZE'), 'threads': threads}


class Instrumented(c.Fixture):
    def __init__(self, output, binary, *, mode, cpu):
        super().__init__(output, binary)
        self.mode, self.cpu = mode, cpu
        self.target_pid = None
        self.resource_rows = []
        self.resource_errors = []
        self.resource_stop = threading.Event()
        self.resource_worker = None
        self.artifact = None

    def stop_group(self):
        if self.resource_worker:
            self.resource_stop.set()
            self.resource_worker.join(timeout=5)
            assert not self.resource_worker.is_alive()
            self.resource_worker = None
        if self.process and self.process.poll() is None:
            if self.target_pid and self.target_pid != self.process.pid and self.target_pid in children(self.process.pid):
                # Let the real workload exit normally before its profiler, so
                # perf/heaptrack can finalize their data instead of being interrupted.
                with contextlib.suppress(ProcessLookupError):
                    os.kill(self.target_pid, signal.SIGINT)
            else:
                os.killpg(self.process.pid, signal.SIGINT)
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
                raise AssertionError('owned profiler group did not finish cleanly')

    def command(self, command, directory):
        prefix = []
        if self.mode == 'normalized':
            prefix = ['taskset', '-c', str(self.cpu)]
        elif self.mode == 'cpu':
            self.artifact = directory / 'cpu.data'
            prefix = ['perf', 'record', '-e', 'cpu-clock:u', '-F', '199', '--call-graph', 'dwarf',
                      '--switch-events', '-o', str(self.artifact), '--']
        elif self.mode == 'offcpu':
            self.artifact = directory / 'offcpu.data'
            prefix = ['perf', 'record', '-e', 'cpu-clock:u', '-F', '99', '--call-graph', 'dwarf',
                      '--switch-events', '-o', str(self.artifact), '--']
        elif self.mode == 'heap':
            self.artifact = directory / 'heap'
            prefix = ['heaptrack', '-o', str(self.artifact)]
        elif self.mode == 'syscalls':
            self.artifact = directory / 'syscalls.txt'
            prefix = ['strace', '-f', '-ttt', '-T', '-s', '0',
                      '-e', 'trace=read,write,writev,sendto,sendmsg,recvfrom,recvmsg,epoll_wait,epoll_pwait,futex',
                      '-o', str(self.artifact)]
        else:
            raise ValueError(self.mode)
        return prefix + command

    def launch(self, target, directory):
        self.stop_group()
        self.resource_rows, self.resource_errors = [], []
        self.resource_stop = threading.Event()
        def spawn(command, **kwargs):
            environment = dict(os.environ)
            if self.mode == 'normalized' and target.startswith('rustybouncer'):
                environment['TOKIO_WORKER_THREADS'] = '1'
            argv = self.command(command, directory)
            dump(directory / 'instrumented-command.json', {'argv': argv, 'TOKIO_WORKER_THREADS': environment.get('TOKIO_WORKER_THREADS')})
            return REAL_POPEN(argv, **kwargs, env=environment, start_new_session=True)
        # Only wrap the real target launch; no metrics, query or result is mocked.
        # Readiness remains a real wire-protocol operation against that process.
        with patch.object(c.subprocess, 'Popen', side_effect=spawn):
            super().launch(target, directory)
        if target == 'direct':
            return
        assert self.process is not None
        process = self.process
        expected = self.binary.resolve() if target.startswith('rustybouncer') else Path(os.environ['PERF_PGBOUNCER_BIN']).resolve()
        def find_target():
            for pid in children(process.pid):
                try:
                    if Path(f'/proc/{pid}/exe').resolve() == expected:
                        return pid
                except FileNotFoundError:
                    pass
            return None
        self.target_pid = c.m.f.eventually(find_target, description='owned target PID')
        first = process_info(self.target_pid)
        if self.mode == 'normalized':
            assert first['threads'] and all(t['affinity'] == [self.cpu] for t in first['threads'])
            if target.startswith('rustybouncer'):
                assert sum(t['name'].startswith('tokio-runtime') for t in first['threads']) == 1
        dump(directory / 'placement.json', {'proxy': first, 'harness_affinity': sorted(os.sched_getaffinity(0)),
                                           'postgres_affinity': self.postgres_affinity()})
        self.resource_worker = threading.Thread(target=self.resources)
        self.resource_worker.start()

    def postgres_affinity(self):
        pid = int(c.m.f.docker('inspect', self.nodes[0], '--format', '{{.State.Pid}}'))
        return sorted(os.sched_getaffinity(pid))

    def resources(self):
        while not self.resource_stop.is_set():
            try:
                self.resource_rows.append(process_info(self.target_pid))
            except Exception as error:
                self.resource_errors.append(repr(error))
            self.resource_stop.wait(.25)

    def finish(self, directory, result):
        self.stop_group()
        dump(directory / 'resources.json', {'samples': self.resource_rows, 'errors': self.resource_errors})
        assert not self.resource_errors, self.resource_errors
        rows = [r for r in self.resource_rows if result['measurement_started_monotonic'] <= r['monotonic'] <= result['measurement_finished_monotonic']]
        if self.target_pid is not None and rows:
            result['cpu_seconds_observed'] = rows[-1]['cpu_seconds'] - rows[0]['cpu_seconds']
            result['rss_peak_bytes_observed'] = max(r['rss_bytes'] for r in rows)
            result['observed_cpu_window_seconds'] = rows[-1]['monotonic'] - rows[0]['monotonic']
        dump(directory / 'result.json', result)

    def close(self):
        try:
            self.stop_group()
        finally:
            super().close()



def timed_arm(fixture, output, clients, repetition, target):
    real_pgbench = c.m.pgbench
    def measured_call(active, count, seconds, directory, measured):
        before = process_info(active.target_pid) if measured and active.target_pid else None
        result = real_pgbench(active, count, seconds, directory, measured)
        if before is not None:
            after = process_info(active.target_pid)
            result['cpu_seconds_including_connect'] = after['cpu_seconds'] - before['cpu_seconds']
            result['cpu_seconds_per_q_including_connect'] = result['cpu_seconds_including_connect'] / result['transactions']
            dump(directory / 'measurement-resource-boundaries.json', {'before': before, 'after': after})
        return result
    with patch.object(c.m, 'pgbench', side_effect=measured_call):
        return c.run_arm(fixture, output, clients, repetition, target)



def syscall_totals(path):
    totals = {}
    with path.open() as source:
        for line in source:
            # Unfinished entries have no result; count only the completed or
            # resumed record, so every syscall is counted exactly once.
            if ' = ' not in line:
                continue
            match = re.search(r'(?:\b(\w+)\(|<\.\.\. (\w+) resumed>)', line)
            returned = re.search(r' = (-?\d+)', line)
            if match is None or returned is None:
                continue
            name = match[1] or match[2]
            row = totals.setdefault(name, {'calls': 0, 'returned_bytes': 0, 'errors': 0, 'aggregate_wall_seconds': 0.0})
            row['calls'] += 1
            value = int(returned[1])
            row['errors'] += int(value < 0)
            if name in ('write','writev','sendto','sendmsg','read','recvfrom','recvmsg'):
                row['returned_bytes'] += max(0,value)
            duration = re.search(r'<([0-9.]+)>$', line.rstrip())
            if duration:
                row['aggregate_wall_seconds'] += float(duration[1])
    return totals


def report_profile(fixture, directory):
    if fixture.mode in ('cpu', 'offcpu'):
        for args, name in [(['perf', 'report', '--stdio', '--no-children', '-i', str(fixture.artifact)], 'report.txt'),
                           (['perf', 'script', '--show-switch-events', '-i', str(fixture.artifact)], 'stacks.txt')]:
            result = subprocess.run(args, text=True, capture_output=True, timeout=90, check=True)
            (directory / name).write_text(result.stdout + result.stderr)
    elif fixture.mode == 'syscalls':
        totals = syscall_totals(fixture.artifact)
        assert totals, 'no completed syscalls captured'
        dump(directory / 'syscalls.json', totals)
    elif fixture.mode == 'heap':
        files = list(directory.glob('heap*.gz')) + list(directory.glob('heap*.zst'))
        assert len(files) == 1, files
        result = subprocess.run(['heaptrack_print', '-f', str(files[0]), '-n', '30', '-s', '5'],
                                text=True, capture_output=True, timeout=120, check=True)
        (directory / 'allocations.txt').write_text(result.stdout + result.stderr)


def profile_arm(fixture, clients, target, directory):
    directory.mkdir()
    fixture.launch(target, directory)
    c.m.preflight(fixture, clients)
    with contextlib.closing(fixture.connect()) as client:
        assert client.query('SELECT payload FROM comparison WHERE id = 42') == [[hashlib.md5(b'42', usedforsecurity=False).hexdigest()*4]]
    command = ['pgbench','-h','127.0.0.1','-p',str(fixture.proxy_port),'-U','postgres','-d','postgres',
               '-n','-M','simple','-c',str(clients),'-j',str(clients),
               '-f',str(fixture.output.parent / 'select.sql')]
    command += ['-T','10'] if fixture.mode in ('cpu','offcpu') else ['-t','1000']
    dump(directory / 'load-command.json',command)
    started = time.monotonic()
    try:
        measured = subprocess.run(command,text=True,capture_output=True,timeout=120)
    except subprocess.TimeoutExpired as error:
        (directory / 'pgbench.txt').write_text(str(error.stdout or '')+str(error.stderr or ''))
        raise
    ended = time.monotonic()
    (directory / 'pgbench.txt').write_text(measured.stdout + measured.stderr)
    assert measured.returncode == 0, measured.stdout + measured.stderr
    processed = re.search(r'number of transactions actually processed:\s*(\d+)',measured.stdout)
    failed = re.search(r'number of failed transactions:\s*(\d+)',measured.stdout)
    assert processed and failed and int(failed[1]) == 0
    completed = int(processed[1])
    assert completed > 0
    if fixture.mode in ('heap','syscalls'):
        assert completed == clients * 1000
    result = {'mode': fixture.mode, 'target': target, 'clients': clients, 'queries': completed,
              'measurement_started_monotonic': started, 'measurement_finished_monotonic': ended,
              'profile_seconds': ended-started, 'errors': 0, 'not_timing_comparable': True,
              'amortization_scope': 'profiler lifetime includes startup, readiness, preflight and probes; divide by controlled pgbench Q count as an upper bound, not steady-state cost'}
    fixture.finish(directory, result)
    report_profile(fixture, directory)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--profile-binary', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    import shutil
    reference = shutil.which('pgbouncer')
    assert reference
    os.environ['PERF_PGBOUNCER_BIN'] = reference
    cpu = min(os.sched_getaffinity(0))
    dump(output / 'environment.json', {'cpu': cpu, 'harness_affinity': sorted(os.sched_getaffinity(0)),
        'binary': str(args.binary.resolve()), 'binary_sha256': hashlib.sha256(args.binary.read_bytes()).hexdigest(),
        'profile_binary': str(args.profile_binary.resolve()), 'profile_sha256': hashlib.sha256(args.profile_binary.read_bytes()).hexdigest(),
        'pgbouncer': reference, 'pgbouncer_sha256': hashlib.sha256(Path(reference).read_bytes()).hexdigest(),
        'normalized_arms': 30, 'attribution_arms': 16, 'profiling_timing_separated': True,
        'platform': platform.platform(), 'logical_cpus': os.cpu_count(), 'load_start': os.getloadavg(),
        'fd_limit': resource.getrlimit(resource.RLIMIT_NOFILE),
        'hardware': {key: next((line.split(':',1)[1].strip() for line in Path('/proc/cpuinfo').read_text().splitlines() if line.startswith(key+'\t')), '')
                     for key in ('model name','cpu family','model','stepping','cpu cores','siblings')},
        'tools': {name: subprocess.check_output([name,'--version'],text=True,stderr=subprocess.STDOUT).strip()
                  for name in ('perf','strace','heaptrack')},
        'perf_event_paranoid': Path('/proc/sys/kernel/perf_event_paranoid').read_text().strip()})
    (output / 'select.sql').write_text('\\set id random(1, 10000)\nSELECT payload FROM comparison WHERE id = :id;\n')
    normalized = Instrumented(output, args.binary.resolve(), mode='normalized', cpu=cpu)
    results = []
    try:
        normalized.start()
        for clients in (10,100):
            for repetition in (1,2,3):
                for target in c.order(repetition,clients):
                    normalized.target_pid = None
                    result = timed_arm(normalized,output,clients,repetition,target)
                    normalized.finish(output / f'c{clients}-r{repetition}-{target}',result)
                    results.append(result)
                    dump(output / 'normalized.json', results)
                    print(f'normalized {len(results)}/30 {target} {clients}',flush=True)
    finally:
        normalized.close()
    profiles = []
    for mode in ('cpu','offcpu','heap','syscalls'):
        directory = output / mode
        directory.mkdir()
        fixture = Instrumented(directory,args.profile_binary.resolve(),mode=mode,cpu=cpu)
        try:
            fixture.start()
            for clients in (10,100):
                for target in ('rustybouncer-session','rustybouncer-routed'):
                    result = profile_arm(fixture,clients,target,directory/f'c{clients}-{target}')
                    profiles.append(result)
                    dump(output / 'profiles.json',profiles)
                    print(f'profile {len(profiles)}/16 {mode} {target} {clients}',flush=True)
        finally:
            fixture.close()
    assert len(results)==30 and len(profiles)==16


if __name__=='__main__':
    main()
