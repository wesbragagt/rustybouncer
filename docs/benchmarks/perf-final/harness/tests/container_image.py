"""H06/G07 container checkpoint: live SQL, rootless identity and distroless contents."""
import argparse
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
import uuid
from pathlib import Path

from failover import Pg, eventually

RUNTIME = 'docker'
IMAGE = 'rustybouncer:latest'
REQUIRE_ROOTLESS = False


def runtime(*args):
    try:
        return subprocess.check_output([RUNTIME, *args], text=True, stderr=subprocess.STDOUT, timeout=60).strip()
    except subprocess.CalledProcessError as error:
        raise RuntimeError(error.output) from error


class ContainerImage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        info = json.loads(runtime('info', '--format', '{{json .}}'))
        rootless = (info.get('host', {}).get('security', {}).get('rootless', False)
                    or any('rootless' in option for option in info.get('SecurityOptions', [])))
        if REQUIRE_ROOTLESS and not rootless:
            raise AssertionError('a rootless runtime is required for this invocation')
        print(f'runtime={RUNTIME}, rootless={rootless}, image={IMAGE}', flush=True)
        cls.prefix = 'rustybouncer-image-' + uuid.uuid4().hex[:10]
        cls.network = cls.prefix + '-net'
        cls.postgres = cls.prefix + '-pg'
        cls.proxy = cls.prefix + '-proxy'
        runtime('network', 'create', cls.network)
        cls.addClassCleanup(runtime, 'network', 'rm', cls.network)
        cls.addClassCleanup(runtime, 'rm', '-f', cls.postgres)
        runtime('run', '-d', '--name', cls.postgres, '--network', cls.network,
                '--network-alias', 'postgres', '-e', 'POSTGRES_HOST_AUTH_METHOD=trust',
                'docker.io/library/postgres:16')
        eventually(lambda: runtime('exec', cls.postgres, 'pg_isready', '-U', 'postgres'))
        cls.directory = tempfile.TemporaryDirectory(prefix=cls.prefix)
        cls.addClassCleanup(cls.directory.cleanup)
        config = Path(cls.directory.name) / 'config.toml'
        config.write_text((Path(__file__).resolve().parents[1] / 'deploy/container.toml').read_text())
        config.chmod(0o644)
        cls.addClassCleanup(runtime, 'rm', '-f', cls.proxy)
        runtime('run', '-d', '--name', cls.proxy, '--network', cls.network,
                '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                '-p', '127.0.0.1::6432', '-v', f'{config}:/etc/rustybouncer/rustybouncer.toml:ro', IMAGE)
        cls.port = int(runtime('port', cls.proxy, '6432/tcp').rsplit(':', 1)[1])
        def ready():
            client = Pg(cls.port)
            try:
                return client.query('SELECT 1') == [['1']]
            finally:
                client.close()
        eventually(ready)

    def test_sql_through_read_only_container(self):
        client = Pg(self.port)
        self.addCleanup(client.close)
        self.assertEqual(client.query('SELECT 42'), [['42']])
        self.assertEqual(client.query('WITH value AS (SELECT 7 AS n) SELECT n FROM value'), [['7']])
        client.query('CREATE TEMP TABLE image_check (n int)')
        client.query('INSERT INTO image_check VALUES (9)')
        self.assertEqual(client.query('SELECT n FROM image_check'), [['9']])

    def test_nonroot_process_without_capabilities(self):
        image = json.loads(runtime('image', 'inspect', IMAGE))[0]
        self.assertEqual(image['Config']['User'], '10001:10001')
        container = json.loads(runtime('inspect', self.proxy))[0]
        pid = container['State']['Pid']
        status = dict(line.split(':', 1) for line in Path(f'/proc/{pid}/status').read_text().splitlines() if ':' in line)
        for field in ('Uid', 'Gid'):
            host_id = int(status[field].split()[0])
            mapping = [list(map(int, line.split())) for line in Path(f'/proc/{pid}/{field.lower()}_map').read_text().splitlines()]
            inside_id = next(inside + host_id - outside for inside, outside, count in mapping if outside <= host_id < outside + count)
            self.assertEqual(inside_id, 10001)
            print(f'container {field}={inside_id}, host {field}={host_id}', flush=True)
        self.assertTrue(container['HostConfig']['ReadonlyRootfs'])
        self.assertEqual(int(status['CapEff'].strip(), 16), 0)
        self.assertEqual(status['NoNewPrivs'].strip(), '1')
        print('read-only root filesystem, CapEff=0, NoNewPrivs=1', flush=True)

    def test_distroless_filesystem(self):
        forbidden = {'sh', 'bash', 'dash', 'ash', 'busybox', 'apt', 'apt-get', 'dpkg',
                     'rpm', 'apk', 'yum', 'dnf', 'nix', 'nix-store', 'cargo', 'rustc'}
        with tempfile.TemporaryFile() as archive:
            subprocess.run([RUNTIME, 'export', self.proxy], stdout=archive, check=True, timeout=60)
            archive.seek(0)
            with tarfile.open(fileobj=archive) as filesystem:
                files = {item.name for item in filesystem if item.isfile() or item.issym()}
        self.assertTrue(any(name.endswith('/bin/rustybouncer') for name in files))
        self.assertEqual({name for name in files if Path(name).name in forbidden}, set())
        self.assertNotIn('var/run/docker.sock', files)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', choices=['docker', 'podman'], default=os.environ.get('CONTAINER_RUNTIME', 'docker'))
    parser.add_argument('--image', default='rustybouncer:latest')
    parser.add_argument('--require-rootless', action='store_true')
    args, unittest_args = parser.parse_known_args()
    RUNTIME, IMAGE, REQUIRE_ROOTLESS = args.runtime, args.image, args.require_rootless
    unittest.main(argv=[__file__, *unittest_args], verbosity=2)
