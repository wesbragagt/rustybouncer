import asyncio

import psycopg
from psycopg import sql

from .base import Case, capability


class Transactions(Case):
    async def insert(self, client, key=1, value="owned"):
        return await client.query(sql.SQL("INSERT INTO {} VALUES (%s, %s)").format(self.table), (key, value))

    async def count(self):
        return (await self.direct.query(sql.SQL("SELECT count(*) FROM {}").format(self.table)))[0][0]

    @capability("transactions")
    async def test_commit_and_visibility(self):
        client = await self.connect()
        await client.query("BEGIN ISOLATION LEVEL READ COMMITTED")
        pid = await client.pid()
        await self.insert(client)
        self.assertEqual(await self.count(), 0, "uncommitted row visible through direct PostgreSQL")
        self.assertEqual(await client.pid(), pid, "transaction backend changed")
        await client.query("COMMIT")
        self.assertEqual(await self.count(), 1)

    @capability("transactions")
    async def test_rollback_discards_writes(self):
        client = await self.connect()
        await client.query("BEGIN")
        await self.insert(client)
        await client.query("ROLLBACK")
        self.assertEqual(await self.count(), 0)

    @capability("transactions")
    async def test_savepoint_and_error_recovery(self):
        client = await self.connect()
        await client.query("BEGIN")
        await self.insert(client)
        await client.query("SAVEPOINT checkpoint")
        with self.assertRaises(psycopg.errors.UniqueViolation):
            await self.insert(client)
        with self.assertRaises(psycopg.errors.InFailedSqlTransaction):
            await client.query("SELECT 1")
        await client.query("ROLLBACK TO SAVEPOINT checkpoint")
        await self.insert(client, 2)
        await client.query("COMMIT")
        self.assertEqual(await self.count(), 2)
        self.assertEqual(await client.query("SELECT 7"), [(7,)])

    @capability("transactions")
    async def test_disconnect_rolls_back_and_does_not_contaminate(self):
        client = await self.connect()
        await client.query("BEGIN")
        await self.insert(client)
        pid = await client.pid()
        await client.close()
        async def released():
            rows = await self.direct.query("SELECT state FROM pg_stat_activity WHERE pid=%s", (pid,))
            return not rows or rows[0][0] not in ("idle in transaction", "idle in transaction (aborted)")
        await self.eventually(released, "disconnected transaction rollback")
        self.assertEqual(await self.count(), 0)
        replacement = await self.connect()
        await self.insert(replacement)
        self.assertEqual(await self.count(), 1)

    @capability("transactions")
    async def test_transaction_local_state_isolated(self):
        count = min(2, int(self.contract.settings.get("pool_size", 2)))
        clients = [await self.connect() for _ in range(count)]
        for index, client in enumerate(clients):
            await client.query("BEGIN")
            await client.query("SET LOCAL application_name = %s", (self.schema + str(index),))
        values = await asyncio.gather(*(client.query("SHOW application_name") for client in clients))
        self.assertEqual(values, [[(self.schema + str(index),)] for index in range(count)])
        for client in clients:
            await client.query("ROLLBACK")

    @capability("transactions")
    async def test_repeatable_read_snapshot(self):
        client = await self.connect()
        await client.query("BEGIN ISOLATION LEVEL REPEATABLE READ")
        query = sql.SQL("SELECT count(*) FROM {}").format(self.table)
        self.assertEqual(await client.query(query), [(0,)])
        await self.insert(self.direct)
        self.assertEqual(await client.query(query), [(0,)])
        await client.query("COMMIT")

    @capability("transactions")
    async def test_serializable_write_conflict(self):
        if int(self.contract.settings.get("pool_size", 2)) < 2:
            self.skipTest("serializable conflict requires capacity for two active transactions")
        await self.insert(self.direct)
        a, b = await self.connect(), await self.connect()
        for client in (a, b):
            await client.query("BEGIN ISOLATION LEVEL SERIALIZABLE")
            await client.query(sql.SQL("SELECT * FROM {}").format(self.table))
        await a.query(sql.SQL("UPDATE {} SET value='a' WHERE id=1").format(self.table))
        await a.query("COMMIT")
        with self.assertRaises(psycopg.errors.SerializationFailure):
            await b.query(sql.SQL("UPDATE {} SET value='b' WHERE id=1").format(self.table))
        await b.query("ROLLBACK")
