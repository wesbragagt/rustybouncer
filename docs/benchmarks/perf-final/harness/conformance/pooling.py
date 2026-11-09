"""Backend ownership is observed, never inferred from allocator rotation."""
import asyncio

from .base import Case, capability


class Pooling(Case):
    async def test_connect_and_unique_results(self):
        client = await self.connect()
        self.assertEqual(await client.query("SELECT %s::text, %s::integer", (self.schema, 42)), [(self.schema, 42)])
        await client.pid()

    @capability("session_affinity")
    async def test_session_affinity(self):
        client = await self.connect()
        first = await client.pid()
        for _ in range(5):
            self.assertEqual(await client.pid(), first, "session unexpectedly changed backend")

    @capability("backend_reuse")
    async def test_backend_reuse_after_disconnect(self):
        limit = int(self.contract.settings["pool_size"])
        seen = []
        for _ in range(limit * 2 + 1):
            client = await self.connect()
            seen.append(await client.pid())
            await client.close()
        self.assertLess(len(set(seen)), len(seen), f"expected reuse with bounded pool; observed pids={seen}")
        self.assertLessEqual(len(set(seen)), limit, f"backend count exceeded declared pool size; pids={seen}")

    async def release(self, finish: str):
        count = int(self.contract.settings["pool_size"])
        holders = [await self.connect() for _ in range(count)]
        for client in holders:
            await client.query("BEGIN")
            await client.pid()
        waiting = await self.connect()
        task = asyncio.create_task(waiting.query("SELECT %s::text", (self.schema,)))
        await self.pending(task)
        await holders[0].query(finish)
        self.assertEqual(await task, [(self.schema,)])
        for client in holders[1:]:
            await client.query("ROLLBACK")

    @capability("release_after_transaction")
    async def test_commit_releases_capacity(self):
        await self.release("COMMIT")

    @capability("release_after_transaction")
    async def test_rollback_releases_capacity(self):
        await self.release("ROLLBACK")

    @capability("release_after_statement")
    async def test_statement_releases_capacity(self):
        count = int(self.contract.settings["pool_size"])
        clients = [await self.connect() for _ in range(count + 1)]
        for client in clients:
            self.assertEqual(await client.query("SELECT 42"), [(42,)])
        identities = [await client.pid() for client in clients]
        self.assertLessEqual(len(set(identities)), count)

    @capability("transactions")
    async def test_simultaneous_transactions_have_distinct_backends(self):
        count = min(3, int(self.contract.settings.get("pool_size", 3)))
        clients = [await self.connect() for _ in range(count)]
        for client in clients:
            await client.query("BEGIN")
        identities = await asyncio.gather(*(client.pid() for client in clients))
        self.assertEqual(len(set(identities)), count, f"concurrent transactions shared backends: {identities}")
        for client in clients:
            await client.query("ROLLBACK")

    @capability("session_state")
    async def test_documented_session_state(self):
        client = await self.connect()
        await client.query("SET application_name = %s", (self.schema,))
        self.assertEqual(await client.query("SHOW application_name"), [(self.schema,)])
        await client.query("RESET application_name")

    async def test_read_only_nested_and_recursive_ctes(self):
        client = await self.connect()
        queries = [
            "WITH x AS (SELECT 1 AS id) SELECT id FROM x",
            "WITH x(id) AS MATERIALIZED (WITH y AS (SELECT 1) SELECT * FROM y) SELECT id FROM x",
            "WITH RECURSIVE x(n) AS (VALUES (1) UNION ALL SELECT n+1 FROM x WHERE n<3) SELECT count(*) FROM x",
        ]
        for query, expected in zip(queries, ([(1,)], [(1,)], [(3,)])):
            self.assertEqual(await client.query(query), expected)
