"""Owned CNPG security fixture; private material is never retained in evidence."""
import base64
import json
import os
import socket
import subprocess
import tempfile
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo

from tests.kind_cluster import FIXTURES, KindCluster, wait_for

APP_USER = 'pool_app'

PASSWORD = 'owned-cnpg-fixture-password'
AUTH_USER = 'rustybouncer_auth'


class SecureCluster(KindCluster):
    def __init__(self, output, image):
        super().__init__(output, image)
        self.private = tempfile.TemporaryDirectory(prefix='rustybouncer-cnpg-tls-')
        self.pem = Path(self.private.name)

    def kind_config(self):
        # Dedicated published NodePorts survive TLS reset/rejection tests; kubectl
        # port-forward terminates its shared tunnel on a single connection reset.
        with socket.socket() as sql_socket, socket.socket() as metrics_socket:
            sql_socket.bind(('127.0.0.1', 0))
            metrics_socket.bind(('127.0.0.1', 0))
            self.port = sql_socket.getsockname()[1]
            self.metrics_port = metrics_socket.getsockname()[1]
        text = (FIXTURES / 'kind.yaml').read_text()
        mappings = ('  - role: control-plane\n    extraPortMappings:\n'
                    f'      - containerPort: 30432\n        hostPort: {self.port}\n        listenAddress: "127.0.0.1"\n'
                    f'      - containerPort: 30090\n        hostPort: {self.metrics_port}\n        listenAddress: "127.0.0.1"\n')
        if text.count('  - role: control-plane\n') != 1:
            raise ValueError('expected exactly one kind control-plane node')
        manifest = self.output / 'kind-nodeports.yaml'
        manifest.write_text(text.replace('  - role: control-plane\n', mappings))
        return manifest

    def expose_router(self):
        self.apply({'apiVersion': 'v1', 'kind': 'Service',
                    'metadata': {'name': 'rustybouncer', 'namespace': 'rustybouncer-test'},
                    'spec': {'type': 'NodePort', 'selector': {'app': 'rustybouncer'},
                             'ports': [{'name': 'postgres', 'port': 6432, 'nodePort': 30432},
                                       {'name': 'metrics', 'port': 9090, 'nodePort': 30090}]}})

    def redact(self, value):
        for password in [PASSWORD, 'other-fixture-password', 'expired-fixture-password',
                         'nologin-fixture-password', 'rotated-owned-fixture-password']:
            value = value.replace(password, '<redacted>')
        return value

    def secret(self, name):
        # KindCluster.run retains stdout. Never route private keys through it.
        command = ['kubectl', '--kubeconfig', str(self.kubeconfig), '-n', 'rustybouncer-test',
                   'get', 'secret', name, '-o', 'json']
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
        return {key: base64.b64decode(value) for key, value in json.loads(result.stdout)['data'].items()}

    def put_secret(self, name, data):
        self.apply({'apiVersion': 'v1', 'kind': 'Secret',
                    'metadata': {'name': name, 'namespace': 'rustybouncer-test'},
                    'data': {key: base64.b64encode(value).decode() for key, value in data.items()}})

    def issue(self, name, cn, ca='client'):
        prefix = self.pem / name
        self.run('openssl', 'req', '-new', '-newkey', 'rsa:2048', '-nodes', '-subj', f'/CN={cn}',
                 '-keyout', str(prefix) + '.key', '-out', str(prefix) + '.csr')
        ext = self.pem / (name + '.ext')
        ext.write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n'
                       'subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\nsubjectAltName=DNS:' + cn
                       + '\nextendedKeyUsage=' + ('serverAuth' if ca == 'frontend' else 'clientAuth') + '\n')
        self.run('openssl', 'x509', '-req', '-in', str(prefix) + '.csr', '-CA', str(self.pem / (ca + '-ca.crt')),
                 '-CAkey', str(self.pem / (ca + '-ca.key')), '-set_serial', str(int.from_bytes(os.urandom(12), 'big')),
                 '-days', '2', '-extfile', str(ext), '-out', str(prefix) + '.crt')
        return {'tls.crt': Path(str(prefix) + '.crt').read_bytes(), 'tls.key': Path(str(prefix) + '.key').read_bytes()}

    def deploy_router(self):
        status = self.status()['certificates']
        client = self.secret(status['clientCASecret'])
        server = self.secret(status['serverCASecret'])
        for key in ['ca.crt', 'ca.key']:
            (self.pem / ('client-' + key)).write_bytes(client[key])
        (self.pem / 'backend-ca.crt').write_bytes(server['ca.crt'])
        self.run('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=owned frontend CA',
                 '-days', '2', '-addext', 'keyUsage=critical,keyCertSign,cRLSign',
                 '-keyout', str(self.pem / 'frontend-ca.key'), '-out', str(self.pem / 'frontend-ca.crt'))
        self.put_secret('router-server-tls', self.issue('server', 'rustybouncer', ca='frontend'))
        self.put_secret('router-client-tls', self.issue('client-one', AUTH_USER))
        self.put_secret('router-ca', {'ca.crt': server['ca.crt']})
        self.sql(f"CREATE ROLE {AUTH_USER} LOGIN; GRANT CONNECT ON DATABASE app TO {AUTH_USER}; "
                 f"CREATE ROLE {APP_USER} LOGIN PASSWORD '{PASSWORD}'; "
                 "CREATE ROLE other_app LOGIN PASSWORD 'other-fixture-password'; "
                 "CREATE ROLE expired_app LOGIN PASSWORD 'expired-fixture-password' VALID UNTIL '2000-01-01'; "
                 "CREATE ROLE nologin_app NOLOGIN PASSWORD 'nologin-fixture-password'; "
                 "CREATE SCHEMA pooler; "
                 "CREATE FUNCTION pooler.auth_query(name) RETURNS TABLE(username name, password text) "
                 "LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog AS "
                 "$$ SELECT rolname, CASE WHEN rolvaliduntil IS NULL OR rolvaliduntil > now() "
                 "THEN rolpassword END FROM pg_authid WHERE rolname=$1 AND rolcanlogin $$; "
                 "REVOKE ALL ON FUNCTION pooler.auth_query(name) FROM PUBLIC; "
                 f"GRANT USAGE ON SCHEMA pooler TO {AUTH_USER}; GRANT EXECUTE ON FUNCTION pooler.auth_query(name) TO {AUTH_USER}")
        self.sql('CREATE DATABASE blocked')
        self.sql('REVOKE CONNECT ON DATABASE blocked FROM PUBLIC')
        rules = [f'hostssl app {AUTH_USER} all cert', 'hostssl all all all scram-sha-256',
                 'hostnossl all all all reject']
        self.kube('-n', 'rustybouncer-test', 'patch', 'cluster/postgres', '--type=merge',
                  '-p', json.dumps({'spec': {'postgresql': {'pg_hba': rules}}}))
        for pod in self.pods():
            name = pod['metadata']['name']
            wait_for(lambda name=name: self.sql("SELECT count(*) FROM pg_hba_file_rules WHERE auth_method='trust' AND type LIKE 'host%'", name) == '0',
                     'CNPG to remove network trust authentication')
        super().deploy_router()

    def router_config(self, config):
        config = config.replace('probe_user = "app"', f'probe_user = "{AUTH_USER}"')
        config = config.replace('startup_timeout_ms = 5000', 'startup_timeout_ms = 15000')
        config = config.replace('probe_timeout_ms = 500', 'probe_timeout_ms = 3000')
        config = config.replace('port = 5432', 'port = 5432\ntls_server_name = "postgres-rw"')
        root, nodes = config.split('[[nodes]]', 1)
        return root + '''
pool_mode = "transaction"
pool_size = 2
pool_server_lifetime_ms = 2000
[frontend_tls]
cert_file = "/server/tls.crt"
key_file = "/server/tls.key"
[backend_tls]
ca_file = "/ca/ca.crt"
cert_file = "/client/tls.crt"
key_file = "/client/tls.key"
[auth_query]
user = "rustybouncer_auth"
query = "SELECT username::text, password FROM pooler.auth_query($1::name)"
database = "app"
''' + '[[nodes]]' + nodes

    def router_deployment(self, deployment):
        pod = deployment['spec']['template']['spec']
        for volume, secret, mount in [('server', 'router-server-tls', '/server'),
                                      ('client', 'router-client-tls', '/client'), ('ca', 'router-ca', '/ca')]:
            pod['volumes'].append({'name': volume, 'secret': {'secretName': secret}})
            pod['containers'][0]['volumeMounts'].append({'name': volume, 'mountPath': mount, 'readOnly': True})
        return deployment

    def connect(self, **changes):
        options = dict(host='rustybouncer', hostaddr='127.0.0.1', port=str(self.port),
                       user=APP_USER, password=PASSWORD, dbname='app', sslmode='verify-full',
                       sslrootcert=str(self.pem / 'frontend-ca.crt'), connect_timeout="20")
        options.update(changes)
        return psycopg.connect(make_conninfo(**options), autocommit=True, prepare_threshold=None)

    def replayed(self):
        target = self.sql('SELECT pg_current_wal_lsn()')
        for pod in self.pods():
            name = pod['metadata']['name']
            wait_for(lambda name=name: self.sql(
                f"SELECT NOT pg_is_in_recovery() OR pg_last_wal_replay_lsn() >= '{target}'::pg_lsn", name) == 't',
                f'role/schema WAL replayed on {name}')

    def password(self, value):
        self.sql(f"ALTER ROLE {APP_USER} PASSWORD '{value}'")
        self.replayed()

    def close(self):
        try:
            super().close()
        finally:
            self.private.cleanup()
