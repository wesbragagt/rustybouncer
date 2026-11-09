import psycopg
from psycopg import sql

from .base import Case, capability


class Recovery(Case):
    async def terminate_owned(self, client):
        # Prove the direct endpoint shares our unique schema before using its PID namespace.
        await client.query(sql.SQL("SELECT count(*) FROM {}").format(self.table))
        pid = await client.pid()
        owners = await self.direct.query("SELECT application_name FROM pg_stat_activity WHERE pid=%s", (pid,))
        self.assertEqual(owners, [(self.schema,)], "refusing to terminate a backend not identified as this test's connection")
        terminated = await self.direct.query("SELECT pg_terminate_backend(%s)", (pid,))
        self.assertEqual(terminated, [(True,)])

    @capability("backend_terminate")
    async def test_backend_termination_and_reconnect(self):
        self.require("transactions")
        client = await self.connect()
        await client.query("BEGIN")
        await self.terminate_owned(client)
        with self.assertRaises(Exception):
            await client.query("SELECT 1")
        replacement = await self.connect()
        self.assertEqual(await replacement.query("SELECT 48"), [(48,)])

    @capability("backend_terminate")
    async def test_interrupted_transaction_never_silently_replayed(self):
        self.require("transactions")
        client = await self.connect()
        await client.query("BEGIN")
        await client.query(sql.SQL("INSERT INTO {} VALUES (1, 'uncommitted')").format(self.table))
        await self.terminate_owned(client)
        with self.assertRaises(Exception):
            await client.query("COMMIT")
        rows = await self.direct.query(sql.SQL("SELECT * FROM {}").format(self.table))
        self.assertEqual(rows, [], "interrupted transaction was committed or replayed")
        replacement = await self.connect()
        self.assertEqual(await replacement.query("SELECT 49"), [(49,)])

    async def recover(self, action: str):
        await self.action(action)
        async def available():
            client = await self.connect()
            try:
                return await client.query("SELECT 50") == [(50,)]
            finally:
                await client.close()
        await self.eventually(available, f"SQL service after {action} without pooler restart")
        # Reconnect direct verification after PostgreSQL restart for cleanup.
        if self.direct.connection.closed:
            self.direct = await self.connect(direct=True)

    @capability("postgres_restart")
    async def test_postgres_restart_recovery(self):
        await self.recover("postgres_restart")

    @capability("pooler_restart")
    async def test_pooler_restart_recovery(self):
        await self.recover("pooler_restart")

    @capability("network_interruption")
    async def test_network_interruption_recovery(self):
        self.require_actions("network_cut", "network_restore")
        self.addAsyncCleanup(self.action, "network_restore")
        await self.action("network_cut")
        client = await self.connect()
        try:
            await client.query("SELECT 1")
        except (psycopg.Error, OSError, TimeoutError):
            pass
        await self.recover("network_restore")

    @capability("backend_terminate")
    async def test_repeated_backend_failures_restore_capacity(self):
        for _ in range(int(self.contract.settings.get("cycles", 3))):
            await self.test_backend_termination_and_reconnect()
            for client in self.clients:
                if client is not self.direct:
                    await client.close()
