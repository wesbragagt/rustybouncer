"""A01-A04/A06-A09/P09: shared pooling with TLS/SCRAM and CNPG in real kind."""
import argparse
import concurrent.futures
import hashlib
import json
import socket
import ssl
import struct
import unittest
import uuid
from pathlib import Path

import psycopg

from conformance.reporting import Result
from tests.cnpg_security_support import APP_USER, AUTH_USER, PASSWORD, SecureCluster
from tests.kind_cluster import wait_for

OUTPUT = Path('/tmp') / ('rustybouncer-cnpg-security-' + uuid.uuid4().hex[:10])
IMAGE = 'rustybouncer:latest'


class CloudNativePGSecurity(unittest.TestCase):
    cluster: SecureCluster

    @classmethod
    def setUpClass(cls):
        cls.cluster = SecureCluster(OUTPUT, IMAGE)
        cls.addClassCleanup(cls.cluster.close)
        cls.cluster.start()

    def test_scram_and_encryption_preserve_database_identity(self):
        with self.cluster.connect() as client:
            self.assertTrue(client.pgconn.ssl_in_use)
            row = client.execute('SELECT current_user, ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()').fetchone()
            self.assertEqual(row, (APP_USER, True))
            self.assertEqual(client.execute('SELECT %s::int', (42,), prepare=True).fetchone(), (42,))

    def test_tls_cancellation_preserves_the_authenticated_session(self):
        with self.cluster.connect() as client, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(client.execute, 'SELECT pg_sleep(30)')
            wait_for(lambda: self.cluster.sql(
                "SELECT count(*) FROM pg_stat_activity WHERE usename='pool_app' "
                "AND query='SELECT pg_sleep(30)' AND state='active'") == '1', 'authenticated query active')
            client.cancel()
            with self.assertRaises(psycopg.errors.QueryCanceled):
                future.result(timeout=10)
            self.assertEqual(client.execute('SELECT 48').fetchone(), (48,))

    def test_wrong_missing_expired_and_nologin_passwords_rejected(self):
        for changes in [dict(password='wrong'), dict(user='absent_app', password='wrong'),
                        dict(user='expired_app', password='expired-fixture-password'),
                        dict(user='nologin_app', password='nologin-fixture-password')]:
            with self.subTest(user=changes.get('user', APP_USER)):
                with self.assertRaises(psycopg.OperationalError) as error:
                    self.cluster.connect(**changes)
                self.assertIn('password authentication failed', str(error.exception))

    def test_unauthorized_database_rejected_before_sql(self):
        with self.assertRaises(psycopg.OperationalError) as error:
            self.cluster.connect(dbname='blocked')
        self.assertIn('startup unavailable', str(error.exception))
        self.assertEqual(self.cluster.sql("SELECT count(*) FROM pg_stat_activity WHERE datname='blocked' AND usename='pool_app'"), '0')

    def test_plaintext_is_rejected_before_sql(self):
        with self.assertRaises(psycopg.OperationalError) as error:
            self.cluster.connect(sslmode='disable')
        self.assertIn('TLS required', str(error.exception))

    def test_untrusted_ca_and_wrong_hostname_rejected(self):
        for changes, reason in [(dict(host='wrong-hostname'), 'does not match host name'),
                                (dict(sslrootcert=str(self.cluster.pem / 'backend-ca.crt')), 'certificate verify failed')]:
            with self.subTest(changes=changes):
                with self.assertRaises(psycopg.OperationalError) as error:
                    self.cluster.connect(**changes)
                self.assertIn(reason, str(error.exception))

    def test_transaction_pool_reuses_backend_and_isolates_users(self):
        with self.cluster.connect() as first, self.cluster.connect() as second:
            first.execute('BEGIN')
            pid = first.execute('SELECT pg_backend_pid()').fetchone()
            self.assertEqual(first.execute('SELECT pg_backend_pid()').fetchone(), pid)
            first.execute('COMMIT')
            second.execute('BEGIN')
            self.assertEqual(second.execute('SELECT pg_backend_pid()').fetchone(), pid)
            second.execute('COMMIT')
        with self.cluster.connect(user='other_app', password='other-fixture-password') as other:
            self.assertEqual(other.execute('SELECT current_user').fetchone(), ('other_app',))

    def test_password_rotation_uses_fresh_auth_query(self):
        rotated = 'rotated-owned-fixture-password'
        self.addCleanup(self.cluster.password, PASSWORD)
        self.cluster.password(rotated)
        with self.assertRaises(psycopg.OperationalError):
            self.cluster.connect()
        with self.cluster.connect(password=rotated) as client:
            self.assertEqual(client.execute('SELECT 43').fetchone(), (43,))

    def peer_certificate(self):
        context = ssl.create_default_context(cafile=str(self.cluster.pem / 'frontend-ca.crt'))
        with socket.create_connection(('127.0.0.1', self.cluster.port), timeout=10) as sock:
            sock.sendall(struct.pack('!II', 8, 80877103))
            self.assertEqual(sock.recv(1), b'S')
            with context.wrap_socket(sock, server_hostname='rustybouncer') as secure:
                certificate = secure.getpeercert(binary_form=True)
                assert certificate is not None
                return hashlib.sha256(certificate).hexdigest()

    def test_mounted_client_and_server_certificates_rotate_without_restart(self):
        pods = json.loads(self.cluster.kube('-n', 'rustybouncer-test', 'get', 'pods', '-l', 'app=rustybouncer', '-o', 'json'))['items']
        uid = pods[0]['metadata']['uid']
        old = self.peer_certificate()
        self.cluster.put_secret('router-server-tls', self.cluster.issue('server-two', 'rustybouncer', ca='frontend'))
        wait_for(lambda: self.peer_certificate() != old, 'projected frontend certificate to rotate', seconds=180)
        # A different certificate CN initially breaks cert-authenticated auth_query.
        # Recovery after restoring the correct CN proves the mounted client files
        # are reloaded and are actually used, rather than ignored/cached forever.
        self.cluster.put_secret('router-client-tls', self.cluster.issue('client-wrong', 'wrong_identity'))
        def rejected():
            try:
                with self.cluster.connect():
                    return False
            except psycopg.OperationalError as error:
                return 'password authentication failed' in str(error)
        wait_for(rejected, 'wrong client certificate to fail auth_query', seconds=180)
        self.cluster.put_secret('router-client-tls', self.cluster.issue('client-two', AUTH_USER))
        def recovered():
            with self.cluster.connect() as client:
                return client.execute('SELECT 44').fetchone() == (44,)
        wait_for(recovered, 'rotated client certificate to restore authenticated SQL', seconds=180)
        after = json.loads(self.cluster.kube('-n', 'rustybouncer-test', 'get', 'pods', '-l', 'app=rustybouncer', '-o', 'json'))['items']
        self.assertEqual(after[0]['metadata']['uid'], uid)
        self.assertEqual(after[0]['status']['containerStatuses'][0]['restartCount'], 0)
        self.cluster.record('certificates-rotated', pod_uid=uid, old_server_sha256=old, new_server_sha256=self.peer_certificate())

    def test_secure_shared_pool_recovers_after_cnpg_promotion(self):
        self.addCleanup(self.cluster.restore)
        self.cluster.sql(f'CREATE TABLE secure_promotion (id int); ALTER TABLE secure_promotion OWNER TO {APP_USER}')
        self.cluster.replayed()
        with self.cluster.connect() as client:
            old = self.cluster.crash_primary()
            new = self.cluster.promoted(old)
            client.execute('INSERT INTO secure_promotion VALUES (47)')
            self.assertEqual(self.cluster.sql('SELECT count(*) FROM secure_promotion', new), '1')
        with self.cluster.connect() as fresh:
            self.assertEqual(fresh.execute('SELECT 47').fetchone(), (47,))
        self.cluster.record('secure-promotion', old=old, new=new)


def main():
    global OUTPUT, IMAGE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=OUTPUT)
    parser.add_argument('--image', default=IMAGE)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output directory already exists; preserve prior evidence')
    OUTPUT, IMAGE = args.output, args.image
    try:
        result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(CloudNativePGSecurity))
    finally:
        CloudNativePGSecurity.doClassCleanups()
    assert isinstance(result, Result)
    result.write(OUTPUT / 'results.json', OUTPUT / 'junit.xml', {'suite': 'kind-cnpg-security', 'image': IMAGE})
    return int(not result.wasSuccessful())


if __name__ == '__main__':
    raise SystemExit(main())
