"""Owned metrics fixture and strict exposition oracle; no runtime imports."""

import contextlib
import http.client
import itertools
import json
import math
import os
import re
import socket
import subprocess
import time
from pathlib import Path

import failover as f

EVIDENCE = Path(os.environ.get('METRICS_EVIDENCE', 'metrics-evidence')).resolve()
BOUNDS = [.001, .005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, math.inf]
PREFIX = 'rustybouncer_'
DOMAINS = {
    'sessions_completed_total': {'outcome': {'ok', 'error', 'aborted'}},
    'backend_connections_active': {'node': None},
    'backend_connects_total': {'node': None, 'outcome': {'ok', 'error', 'aborted'}},
    'queries_total': {'kind': {'read', 'write', 'transaction', 'stateful', 'unsupported', 'malformed'},
                      'outcome': {'ok', 'sql_error', 'router_error', 'delivery_error', 'aborted'}},
    'query_attempts_total': {'route': {'primary', 'replica'}, 'outcome': {'ok', 'sql_error', 'error', 'aborted'}},
    'topology_nodes': {'role': {'primary', 'replica', 'unavailable'}},
    'node_role': {'node': None, 'role': {'primary', 'replica', 'unavailable'}},
    'probes_total': {'node': None, 'outcome': {'ok', 'error'}},
    'build_info': {'version': None},
    'pooling_mode': {'mode': {'none','session','transaction'}},
    'pool_resets_total': {'outcome': {'ok','error','aborted'}},
}
HISTOGRAMS = {'session_duration_seconds': {}, 'query_duration_seconds': {},
              'route_wait_duration_seconds': {}, 'pool_reset_duration_seconds': {}, 'probe_duration_seconds': {'node': None}}
COUNTERS = {'clients_accepted_total', 'clients_rejected_total', 'sessions_completed_total',
            'backend_connects_total', 'queries_total', 'query_attempts_total', 'read_retries_total',
            'topology_changes_total', 'primary_changes_total', 'probes_total', 'control_requests_total', 'result_buffer_rejections_total',
            'pool_reuses_total','pool_discards_total','pool_resets_total'}
GAUGES = {'routing_enabled', 'build_info', 'clients_active', 'client_capacity',
          'backend_connections_active', 'queries_in_flight', 'topology_nodes', 'node_role',
          'topology_ambiguous', 'topology_discovery_completed', 'topology_last_completed_timestamp_seconds', 'result_buffer_limit_bytes',
          'result_buffer_reserved_bytes', 'result_buffer_highwater_bytes', 'result_buffer_allocated_bytes','pooling_enabled','pooling_mode','pool_backend_capacity','pool_size'}


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def request(port, method='GET', path='/metrics'):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        return response.status, response.getheader('Content-Type', ''), response.read().decode()
    finally:
        connection.close()


class Snapshot:
    def __init__(self, text):
        self.text = text
        self.samples = {}
        self.types = {}
        self.help = set()
        for line in text.splitlines():
            if line.startswith('# TYPE '):
                _, _, name, kind = line.split()
                self.types[name.removeprefix(PREFIX)] = kind
            elif line.startswith('# HELP '):
                self.help.add(line.split()[2].removeprefix(PREFIX))
            elif line and not line.startswith('#'):
                self.parse_sample(line)

    def parse_sample(self, line):
        match = re.fullmatch(r'(\w+)(\{.*\})?\s+([^\s]+)', line)
        assert match, f'invalid sample: {line}'
        name, raw, value = match.groups()
        labels = {}
        if raw:
            position = 1
            pattern = re.compile(r'(\w+)="((?:[^"\\]|\\[\\"n])*)"(?:,|(?=\}))')
            while position < len(raw) - 1:
                item = pattern.match(raw, position)
                assert item, f'invalid labels: {raw}'
                key, escaped = item.groups()
                assert key not in labels
                labels[key] = re.sub(r'\\([\\"n])', lambda m: '\n' if m[1] == 'n' else m[1], escaped)
                position = item.end()
        assert name.startswith(PREFIX), name
        key = (name.removeprefix(PREFIX), tuple(sorted(labels.items())))
        assert key not in self.samples, f'duplicate {key}'
        number = float(value)
        assert math.isfinite(number) and number >= 0, line
        self.samples[key] = number

    def value(self, name, **labels):
        return self.samples[(name, tuple(sorted(labels.items())))]

    def total(self, name, **labels):
        return sum(value for (family, pairs), value in self.samples.items()
                   if family == name and all(dict(pairs).get(k) == v for k, v in labels.items()))

    def validate(self, nodes):
        expected = {**dict.fromkeys(COUNTERS, 'counter'), **dict.fromkeys(GAUGES, 'gauge'),
                    **dict.fromkeys(HISTOGRAMS, 'histogram')}
        assert self.types == expected, (self.types, expected)
        assert self.help == set(expected)
        assert len(self.samples) == 154 + 29 * len(nodes), len(self.samples)
        for (name, pairs), _ in self.samples.items():
            base = re.sub(r'_(bucket|sum|count)$', '', name) if name not in expected else name
            domains = dict(DOMAINS.get(base, HISTOGRAMS.get(base, {})))
            if name.endswith('_bucket'):
                domains['le'] = None
            labels = dict(pairs)
            assert set(labels) == set(domains), (name, labels, domains)
            for key, allowed in domains.items():
                if key == 'node':
                    allowed = set(nodes)
                if allowed is not None:
                    assert labels[key] in allowed, (name, labels)
        self.validate_domains(nodes)
        for family, domains in HISTOGRAMS.items():
            groups = [{'node': node} for node in nodes] if domains else [{}]
            for labels in groups:
                buckets = sorted((float(dict(pairs)['le']), value)
                                 for (name, pairs), value in self.samples.items()
                                 if name == family + '_bucket' and all(dict(pairs).get(k) == v for k, v in labels.items()))
                assert [bound for bound, _ in buckets] == BOUNDS
                values = [value for _, value in buckets]
                assert values == sorted(values)
                assert values[-1] == self.value(family + '_count', **labels)

    def validate_domains(self, nodes):
        for family, domains in DOMAINS.items():
            if family == 'build_info':
                assert sum(name == family for name, _ in self.samples) == 1
                assert self.total(family) == 1
                continue
            keys = list(domains)
            values = [nodes if key == 'node' else domains[key] or () for key in keys]
            for combination in itertools.product(*values):
                assert self.value(family, **dict(zip(keys, combination))) >= 0

    def monotonic(self, previous):
        assert self.samples.keys() == previous.samples.keys()
        for key, value in self.samples.items():
            if key[0] in COUNTERS or key[0].endswith(('_bucket', '_sum', '_count')):
                assert value >= previous.samples[key], key


class MetricsCluster(f.Cluster):
    """Real physical streaming replicas; capacity is deliberately above 100."""
    names = ('a"\\\nmetrics', 'b', 'c')

    def __init__(self, evidence):
        super().__init__()
        self.evidence = evidence
        self.evidence.mkdir(parents=True, exist_ok=True)
        self.metrics_port = free_port()
        self.proxy_port = free_port()
        self.capacity = 256

    def start(self):
        f.docker('network', 'create', self.prefix)
        self.network_created = True
        primary = self.nodes[0]
        f.docker('run', '-d', '--name', primary, '--network', self.prefix,
                 '-p', '127.0.0.1::5432', '-e', 'POSTGRES_HOST_AUTH_METHOD=trust',
                 f.IMAGE, '-c', 'wal_level=replica', '-c', 'max_wal_senders=10',
                 '-c', 'max_connections=400', '-c', 'cluster_name=a')
        f.eventually(lambda: f.sql(primary, 'SELECT 1') == '1')
        f.docker('exec', primary, 'bash', '-c',
                 'echo "host replication all 0.0.0.0/0 trust" >> "$PGDATA/pg_hba.conf"')
        f.sql(primary, 'SELECT pg_reload_conf()')
        # A separate probe identity makes pg_stat_activity reconciliation
        # independent of a probe's brief active/idle transition at disconnect.
        f.sql(primary, 'CREATE ROLE rustybouncer_metrics_probe LOGIN')
        f.sql(primary, 'CREATE TABLE continuity (id integer PRIMARY KEY, payload text NOT NULL)')
        for node, name in zip(self.nodes[1:], ('b', 'c')):
            f.docker('run', '-d', '--name', node, '--network', self.prefix,
                     '-p', '127.0.0.1::5432', '--user', 'postgres', f.IMAGE, 'bash', '-c',
                     f'pg_basebackup -h {primary} -U postgres -D /tmp/standby -R -X stream && '
                     f'exec postgres -D /tmp/standby -c listen_addresses="*" '
                     f'-c max_connections=400 -c cluster_name={name}')
        for node in self.nodes[1:]:
            f.eventually(lambda node=node: f.sql(node, 'SELECT pg_is_in_recovery()') == 't')
        self.log = self.log_path.open('w')
        self.restart()

    def stop_proxy(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def restart(self, enabled=True, routed=True, capacity=256, wait=True):
        self.stop_proxy()
        config = (f'listen = "127.0.0.1:{self.proxy_port}"\nmax_clients = {capacity}\n'
                  'probe_interval_ms = 100\nprobe_timeout_ms = 300\nwait_timeout_ms = 4000\n'
                  'startup_timeout_ms = 2000\nquery_timeout_ms = 6000\n'
                  'read_retry_timeout_ms = 8000\nread_max_attempts = 3\n'
                  'probe_user = "rustybouncer_metrics_probe"\nprobe_database = "postgres"\n'
                  f'route_selects = {str(routed).lower()}\n')
        if enabled:
            config += f'metrics_listen = "127.0.0.1:{self.metrics_port}"\n'
        config += ''.join(f'[[nodes]]\nname = {json.dumps(name)}\nhost = "127.0.0.1"\nport = {f.port(node)}\n'
                          for name, node in zip(self.names, self.nodes))
        path = Path(self.directory.name) / 'config.toml'
        path.write_text(config)
        (self.evidence / f'config-{time.time_ns()}.toml').write_text(config)
        self.process = subprocess.Popen([str(f.BINARY), str(path)], stdout=self.log, stderr=self.log)
        if wait:
            f.eventually(self.ready)
            if routed:
                f.eventually(self.replicas_discovered)

    def scrape(self):
        status, content, text = request(self.metrics_port)
        assert status == 200 and 'text/plain' in content and 'version=0.0.4' in content
        path = self.evidence / f'scrape-{time.time_ns()}.prom'
        path.write_text(text)
        result = subprocess.run(['promtool', 'check', 'metrics'], input=text, text=True,
                                capture_output=True, check=False)
        path.with_suffix('.promtool.txt').write_text(result.stdout + result.stderr)
        assert result.returncode == 0, result.stdout + result.stderr
        snapshot = Snapshot(text)
        snapshot.validate(self.names)
        return snapshot

    def idle(self):
        def check():
            current = self.scrape()
            return current if current.value('clients_active') == 0 and current.total('backend_connections_active') == 0 and current.value('queries_in_flight') == 0 else None
        return f.eventually(check, description='zero live gauges after quiescence')

    def close(self):
        self.stop_proxy()
        if self.log_path.exists():
            (self.evidence / 'proxy.log').write_text(self.log_path.read_text())
        for index, node in enumerate(self.nodes):
            if index not in self.paused:
                with contextlib.suppress(RuntimeError):
                    (self.evidence / f'postgres-{index}.log').write_text(f.docker('logs', node))
        super().close()
