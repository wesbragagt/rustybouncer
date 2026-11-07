"""Self-contained PostgreSQL deadline/rollback/isolation suite; requires Nix devShell."""
import asyncio
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

from conformance.base import Case
from conformance.client import Client
from conformance.config import Contract


class PostgreSQLDeadlines(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='deadline-postgres-')
        cls.addClassCleanup(cls.directory.cleanup)
        cls.data = Path(cls.directory.name) / 'data'
        cls.addClassCleanup(cls.stop_postgres)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        subprocess.run(['initdb', '-D', str(cls.data), '--auth=trust', '--no-locale', '--encoding=UTF8'],
                       check=True, capture_output=True, timeout=30)
        subprocess.run(['pg_ctl', '-D', str(cls.data), '-l', str(Path(cls.directory.name) / 'postgres.log'),
                        '-o', f'-h 127.0.0.1 -p {port} -k {cls.directory.name}', '-w', 'start'],
                       check=True, capture_output=True, timeout=30)
        cls.dsn = f'host=127.0.0.1 port={port} dbname=postgres user={os.environ["USER"]} sslmode=disable'

    @classmethod
    def stop_postgres(cls):
        # Class cleanups also run when setUpClass fails after starting PostgreSQL.
        if (cls.data / 'postmaster.pid').exists():
            subprocess.run(['pg_ctl', '-D', str(cls.data), '-m', 'immediate', '-w', 'stop'],
                           check=True, capture_output=True, timeout=30)

    async def asyncSetUp(self):
        self.client = await Client.connect(self.dsn, 1, [])
        self.addAsyncCleanup(self.client.close)

    async def test_query_timeout_disconnect_rolls_back(self):
        case = Case()
        case.contract = Contract('postgres-deadlines', 'routing', {}, {'operation_timeout': 1})
        case.pooler_dsn = case.postgres_dsn = self.dsn
        self.addAsyncCleanup(case.drop_schema)
        await case.asyncSetUp()
        await self.client.query('BEGIN')
        await self.client.query('INSERT INTO ' + case.schema + ".records VALUES (1, 'uncommitted')")
        self.client.timeout = .05
        with self.assertRaises(TimeoutError):
            await self.client.query('SELECT pg_sleep(10)')
        self.assertTrue(self.client.connection.closed)
        self.assertEqual(await case.direct.query('SELECT count(*) FROM ' + case.schema + '.records'), [(0,)])

    async def test_caller_cancellation_closes_connection(self):
        task = asyncio.create_task(self.client.query('SELECT pg_sleep(10)'))
        await asyncio.sleep(.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.client.connection.closed)

    async def test_connection_handshake_deadline(self):
        drained = asyncio.Event()
        async def stalled(reader, writer):
            try:
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
                drained.set()
        server = await asyncio.start_server(stalled, '127.0.0.1', 0)
        try:
            port = server.sockets[0].getsockname()[1]
            with self.assertRaises(TimeoutError):
                await Client.connect(f'host=127.0.0.1 port={port} user=deadline sslmode=disable', .05, [])
            await asyncio.wait_for(drained.wait(), 1)
        finally:
            server.close()
            await server.wait_closed()

    async def test_protocol_deadline(self):
        self.client.timeout = .05
        with self.assertRaises(TimeoutError):
            await self.client.protocol(lambda pg: pg.send_query(b'SELECT pg_sleep(10)'))
        self.assertTrue(self.client.connection.closed)

    async def test_fixture_cleanup_rollback_and_schema(self):
        case = Case()
        case.contract = Contract('postgres-deadlines', 'routing', {}, {'operation_timeout': 1})
        case.pooler_dsn = case.postgres_dsn = self.dsn
        self.addAsyncCleanup(case.drop_schema)
        await case.asyncSetUp()
        await case.direct.query('BEGIN')
        await case.direct.query('INSERT INTO ' + case.schema + ".records VALUES (1, 'uncommitted')")
        await case.drop_schema()
        rows = await self.client.query('SELECT count(*) FROM pg_namespace WHERE nspname = %s', (case.schema,))
        self.assertEqual(rows, [(0,)])
        self.assertTrue(all(client.connection.closed for client in case.clients))


if __name__ == '__main__':
    unittest.main(verbosity=2)
