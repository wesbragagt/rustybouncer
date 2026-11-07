import asyncio
import time

from .base import Case, capability


class Limits(Case):
    async def holders(self):
        self.require("transactions")
        clients = [await self.connect() for _ in range(int(self.contract.settings["pool_size"]))]
        pids = []
        for client in clients:
            await client.query("BEGIN")
            pids.append(await client.pid())
        self.assertEqual(len(set(pids)), len(clients))
        return clients

    async def assert_backend_bound(self):
        count = (await self.direct.query(
            "SELECT count(*) FROM pg_stat_activity WHERE application_name=%s AND backend_type='client backend'", (self.schema,)
        ))[0][0]
        self.evidence.append({"test_backend_connections": count})
        self.assertLessEqual(count, int(self.contract.settings["pool_size"]), "declared server pool size exceeded")

    @capability("limits")
    async def test_pool_limit_and_queued_completion(self):
        clients = await self.holders()
        waiting = await self.connect()
        task = asyncio.create_task(waiting.query("SELECT 19"))
        await self.pending(task)
        await self.assert_backend_bound()
        await clients[0].query("ROLLBACK")
        self.assertEqual(await task, [(19,)])
        await self.assert_backend_bound()
        for client in clients[1:]:
            await client.query("ROLLBACK")

    @capability("client_limits")
    async def test_maximum_client_admission(self):
        maximum = int(self.contract.settings["max_clients"])
        clients = [await self.connect() for _ in range(maximum)]
        with self.assertRaises(Exception, msg="client above configured limit unexpectedly connected"):
            await self.connect()
        for client in clients:
            await client.close()
        client = await self.connect()
        self.assertEqual(await client.query("SELECT 1"), [(1,)])

    @capability("wait_timeout")
    async def test_queued_client_timeout(self):
        clients = await self.holders()
        waiting = await self.connect()
        expected = self.contract.number("wait_timeout", 1)
        tolerance = self.contract.number("timing_tolerance", 1)
        started = time.monotonic()
        try:
            await waiting.query("SELECT 1")
            self.fail("expected pool-wait rejection; query unexpectedly completed")
        except (AssertionError, TimeoutError):
            raise
        except Exception as error:
            self.evidence.append({"expected_wait_rejection": type(error).__name__})
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, max(0, expected - tolerance))
        self.assertLessEqual(elapsed, expected + tolerance)
        for client in clients:
            await client.query("ROLLBACK")

    @capability("limits")
    async def test_disconnected_waiter_leaves_queue(self):
        clients = await self.holders()
        abandoned = await self.connect()
        task = asyncio.create_task(abandoned.query("SELECT 21"))
        await self.pending(task)
        abandoned.abort()
        await self.stop_task(task)
        following = await self.connect()
        later = asyncio.create_task(following.query("SELECT 22"))
        await self.pending(later)
        await clients[0].query("ROLLBACK")
        self.assertEqual(await later, [(22,)])
        for client in clients[1:]:
            await client.query("ROLLBACK")

    @capability("limits")
    async def test_repeated_saturation_does_not_leak_capacity(self):
        for _ in range(int(self.contract.settings.get("cycles", 3))):
            await self.test_pool_limit_and_queued_completion()
            for client in list(self.clients):
                if client is not self.direct:
                    await client.close()

    @capability("reserve_pool")
    async def test_reserve_capacity(self):
        clients = await self.holders()
        extra = await self.connect()
        self.assertEqual(await extra.query("SELECT 23"), [(23,)])
        for client in clients:
            await client.query("ROLLBACK")

    @capability("independent_pools")
    async def test_independent_pool_not_blocked(self):
        import os
        dsn = os.environ.get("POOLER_ALT_DATABASE_DSN")
        if not dsn:
            self.skipTest("POOLER_ALT_DATABASE_DSN not provided")
        clients = await self.holders()
        other = await self.connect(dsn=dsn)
        self.assertEqual(await other.query("SELECT 31"), [(31,)])
        for client in clients:
            await client.query("ROLLBACK")

    @capability("cancel")
    async def test_cancelled_waiter_leaves_queue(self):
        self.require("limits")
        clients = await self.holders()
        waiting = await self.connect()
        task = asyncio.create_task(waiting.query("SELECT 24"))
        await self.pending(task)
        await asyncio.wait_for(waiting.connection.cancel_safe(), waiting.timeout)
        with self.assertRaises(Exception):
            await task
        await waiting.close()
        following = await self.connect()
        later = asyncio.create_task(following.query("SELECT 25"))
        await self.pending(later)
        await clients[0].query("ROLLBACK")
        self.assertEqual(await later, [(25,)])
        for client in clients[1:]:
            await client.query("ROLLBACK")
