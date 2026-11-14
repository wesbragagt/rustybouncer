"""Owned kind/CloudNativePG fixture. Never reads or changes the user's kubeconfig."""
import hashlib
import json
import os
import socket
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

import psycopg
from psycopg import ClientCursor

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/kubernetes'


def wait_for(predicate, description, seconds=240):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except (RuntimeError, OSError, psycopg.Error) as error:
            last = str(error)
        time.sleep(min(.2, max(0, deadline - time.monotonic())))
    raise AssertionError(f'expected {description} within {seconds}s; actual last error: {last}')


class KindCluster:
    def __init__(self, output: Path, image: str):
        self.output = output
        output.mkdir(parents=True, exist_ok=False)
        self.name = 'rustybouncer-' + uuid.uuid4().hex[:10]
        self.image = image
        self.kubeconfig = output / 'kubeconfig'
        self.stopped: set[str] = set()
        self.forward = None
        self.created = False
        self.evidence: list[dict] = []
        self.pins = json.loads((FIXTURES / 'pins.json').read_text())
        self.port = 0
        self.metrics_port = 0

    def run(self, *argv, data=None, seconds=60):
        completed = subprocess.run(argv, input=data, text=True, capture_output=True,
                                   timeout=seconds, env={**os.environ, 'KIND_EXPERIMENTAL_PROVIDER': 'docker'})
        with (self.output / 'commands.log').open('a') as log:
            log.write(self.redact(f'$ {argv!r}\n{completed.stdout}{completed.stderr}\n'))
        if completed.returncode:
            raise RuntimeError(f'{argv[:3]} exit {completed.returncode}: {completed.stdout}{completed.stderr}')
        return completed.stdout.strip()

    def redact(self, value):
        return value

    def kube(self, *args, data=None, seconds=30):
        return self.run('kubectl', '--kubeconfig', str(self.kubeconfig),
                        '--request-timeout=20s', *args, data=data, seconds=seconds)

    def apply(self, *resources):
        return self.kube('apply', '-f', '-', data=json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': resources}))

    def kind_config(self):
        return FIXTURES / 'kind.yaml'

    def start(self):
        print(f'creating owned kind cluster {self.name}', flush=True)
        # Mark ownership before creation so partially created nodes are removed too.
        self.created = True
        self.run('kind', 'create', 'cluster', '--name', self.name, '--image', self.pins['node_image'],
                 '--config', str(self.kind_config()), '--kubeconfig', str(self.kubeconfig),
                 '--wait', '180s', seconds=360)
        self.run('kind', 'load', 'docker-image', '--name', self.name, self.image, seconds=180)
        with urllib.request.urlopen(self.pins['operator_url'], timeout=30) as response:
            manifest = response.read()
        if hashlib.sha256(manifest).hexdigest() != self.pins['operator_sha256']:
            raise ValueError('CloudNativePG operator manifest checksum mismatch')
        # Pin the operator image too; the upstream tagged manifest uses Always.
        manifest = manifest.decode().replace(self.pins['operator_image'].split('@')[0], self.pins['operator_image'])
        self.kube('apply', '--server-side', '-f', '-', data=manifest, seconds=60)
        self.kube('-n', 'cnpg-system', 'patch', 'deployment/cnpg-controller-manager', '--type=merge',
                  '-p', json.dumps({'spec': {'template': {'spec': {
                      'nodeSelector': {'node-role.kubernetes.io/control-plane': ''},
                      'tolerations': [{'key': 'node-role.kubernetes.io/control-plane', 'operator': 'Exists', 'effect': 'NoSchedule'}]}}}}))
        self.kube('-n', 'cnpg-system', 'rollout', 'status', 'deployment/cnpg-controller-manager',
                  '--timeout=240s', seconds=260)
        self.kube('create', 'namespace', 'rustybouncer-test')
        # Deployment readiness can precede Service routing to the admission
        # webhook. Applying this owned declarative manifest is idempotent.
        wait_for(lambda: self.kube('apply', '-f', str(FIXTURES / 'postgres.yaml')),
                 'CNPG admission webhook to accept the owned cluster manifest', seconds=90)
        self.healthy()
        self.deploy_router()
        self.record('versions', kind=self.run('kind', 'version'), kubernetes=json.loads(self.kube('version', '-o', 'json')),
                    image_id=self.run('docker', 'image', 'inspect', self.image, '--format', '{{.Id}}'))
        self.record('initial-topology', cluster=self.status(), pods=self.pods())

    def status(self):
        return json.loads(self.kube('-n', 'rustybouncer-test', 'get', 'cluster/postgres', '-o', 'json')).get('status', {})

    def pods(self):
        return json.loads(self.kube('-n', 'rustybouncer-test', 'get', 'pods',
                                    '-l', 'cnpg.io/cluster=postgres,cnpg.io/podRole=instance', '-o', 'json'))['items']

    def primary(self):
        return self.status().get('currentPrimary', '')

    def healthy(self):
        def ready():
            state = self.status()
            return (state.get('readyInstances') == 3 and state.get('phase') == 'Cluster in healthy state'
                    and self.sql("SELECT count(*) FROM pg_stat_replication WHERE state='streaming'", state.get('currentPrimary')) == '2')
        wait_for(ready, 'three healthy CNPG instances with two streaming replicas', seconds=360)

    def sql(self, query, pod=None):
        return self.kube('-n', 'rustybouncer-test', 'exec', pod or self.primary(), '-c', 'postgres',
                         '--', 'psql', '-U', 'postgres', '-d', 'app', '-At', '-v', 'ON_ERROR_STOP=1', '-c', query)

    def deploy_router(self):
        pods = self.pods()
        if len(pods) != 3 or len({pod['spec']['nodeName'] for pod in pods}) != 3:
            raise AssertionError('expected three PostgreSQL instances on distinct worker nodes')
        services = []
        config = (ROOT / 'deploy/container.toml').read_text().split('[[nodes]]')[0]
        config = config.replace('probe_user = "postgres"', 'probe_user = "app"').replace('probe_database = "postgres"', 'probe_database = "app"')
        config = config.replace('probe_interval_ms = 500', 'probe_interval_ms = 100').replace('probe_timeout_ms = 1000', 'probe_timeout_ms = 500')
        config = config.replace('wait_timeout_ms = 10000', 'wait_timeout_ms = 180000').replace('read_max_attempts = 3', 'read_max_attempts = 6')
        config = config.replace('query_timeout_ms = 300000', 'query_timeout_ms = 10000').replace('read_retry_timeout_ms = 600000', 'read_retry_timeout_ms = 180000')
        config += '\nmetrics_listen = "0.0.0.0:9090"\n'
        for pod in pods:
            name = pod['metadata']['name']
            config += f'\n[[nodes]]\nname = "{name}"\nhost = "{name}"\nport = 5432\n'
            services.append({'apiVersion': 'v1', 'kind': 'Service',
                             'metadata': {'name': name, 'namespace': 'rustybouncer-test'},
                             'spec': {'publishNotReadyAddresses': True, 'selector': {'cnpg.io/instanceName': name},
                                      'ports': [{'port': 5432}]}})
        config = self.router_config(config)
        self.apply(*services, {'apiVersion': 'v1', 'kind': 'ConfigMap',
                              'metadata': {'name': 'router', 'namespace': 'rustybouncer-test'}, 'data': {'config.toml': config}})
        deployment = {'apiVersion': 'apps/v1', 'kind': 'Deployment',
                    'metadata': {'name': 'rustybouncer', 'namespace': 'rustybouncer-test'},
                    'spec': {'replicas': 1, 'selector': {'matchLabels': {'app': 'rustybouncer'}}, 'template': {
                        'metadata': {'labels': {'app': 'rustybouncer'}}, 'spec': {
                            'nodeSelector': {'node-role.kubernetes.io/control-plane': ''},
                            'tolerations': [{'key': 'node-role.kubernetes.io/control-plane', 'operator': 'Exists', 'effect': 'NoSchedule'}],
                            'securityContext': {'runAsNonRoot': True, 'runAsUser': 10001, 'runAsGroup': 10001, 'seccompProfile': {'type': 'RuntimeDefault'}},
                            'containers': [{'name': 'router', 'image': self.image, 'imagePullPolicy': 'Never',
                                            'args': ['/config/config.toml'], 'ports': [{'containerPort': 6432}, {'containerPort': 9090}],
                                            'securityContext': {'readOnlyRootFilesystem': True, 'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}},
                                            'readinessProbe': {'tcpSocket': {'port': 6432}, 'periodSeconds': 1},
                                            'volumeMounts': [{'name': 'config', 'mountPath': '/config', 'readOnly': True}]}],
                            'volumes': [{'name': 'config', 'configMap': {'name': 'router'}}]}}}}
        self.apply(self.router_deployment(deployment))
        self.kube('-n', 'rustybouncer-test', 'rollout', 'status', 'deployment/rustybouncer', '--timeout=120s', seconds=140)
        self.expose_router()
        def connected():
            with self.connect() as client:
                return client.execute('SELECT 1').fetchone() == (1,)
        wait_for(connected, 'SQL through in-cluster rustybouncer')

    def expose_router(self):
        with socket.socket() as probe, socket.socket() as metrics_probe:
            probe.bind(('127.0.0.1', 0))
            metrics_probe.bind(('127.0.0.1', 0))
            self.port = probe.getsockname()[1]
            self.metrics_port = metrics_probe.getsockname()[1]
        log = (self.output / 'port-forward.log').open('w')
        try:
            self.forward = subprocess.Popen(['kubectl', '--kubeconfig', str(self.kubeconfig), '-n', 'rustybouncer-test',
                                             'port-forward', 'deployment/rustybouncer', f'{self.port}:6432', f'{self.metrics_port}:9090'], stdout=log, stderr=subprocess.STDOUT)
        finally:
            log.close()
    def router_config(self, config):
        return config

    def router_deployment(self, deployment):
        return deployment

    def connect(self):
        return psycopg.connect(host='127.0.0.1', port=self.port, user='app', dbname='app', sslmode='disable',
                               connect_timeout=5, autocommit=True, cursor_factory=ClientCursor, prepare_threshold=None)

    def crash_primary(self):
        primary = self.primary()
        node = next(pod['spec']['nodeName'] for pod in self.pods() if pod['metadata']['name'] == primary)
        if not node.startswith(self.name + '-worker'):
            raise AssertionError(f'refusing to crash unowned/non-worker node {node}')
        self.stopped.add(node)
        self.run('docker', 'kill', '--signal=KILL', node)
        self.record('primary-node-stopped', primary=primary, node=node)
        return primary

    def promoted(self, previous):
        def changed():
            current = self.primary()
            return current if current and current != previous and self.sql('SELECT pg_is_in_recovery()', current) == 'f' else None
        primary = wait_for(changed, 'CNPG to promote a different PostgreSQL instance')
        self.record('promotion', previous=previous, primary=primary, cluster=self.status())
        return primary

    def restore(self):
        for node in list(self.stopped):
            self.run('docker', 'start', node)
            self.stopped.remove(node)
        self.healthy()

    def record(self, event, **fields):
        entry = {'event': event, 'time': time.time(), **fields}
        self.evidence.append(entry)
        with (self.output / 'events.jsonl').open('a') as stream:
            stream.write(json.dumps(entry) + '\n')

    def diagnostics(self):
        for name, args in [('cluster', ['get', 'cluster/postgres', '-o', 'json']),
                           ('pods', ['get', 'pods', '-o', 'json']), ('events', ['get', 'events', '-o', 'json']),
                           ('router', ['logs', 'deployment/rustybouncer', '--tail=200'])]:
            try:
                (self.output / f'{name}.log').write_text(self.kube('-n', 'rustybouncer-test', *args))
            except Exception as error:
                self.record('diagnostic-error', name=name, error=str(error))

    def close(self):
        errors = []
        if self.forward is not None:
            self.forward.terminate()
            try:
                self.forward.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.forward.kill()
                self.forward.wait(timeout=10)
        for node in list(self.stopped):
            try:
                self.run('docker', 'start', node)
                self.stopped.remove(node)
            except Exception as error:
                errors.append(error)
        if self.created:
            self.diagnostics()
            try:
                self.run('kind', 'delete', 'cluster', '--name', self.name, '--kubeconfig', str(self.kubeconfig), seconds=120)
            except Exception as error:
                errors.append(error)
        self.kubeconfig.unlink(missing_ok=True)
        if errors:
            raise ExceptionGroup('kind cluster cleanup failures', errors)
