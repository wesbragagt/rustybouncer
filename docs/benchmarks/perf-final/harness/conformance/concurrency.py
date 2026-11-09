import asyncio
import math
import time

from .base import Case


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * quantile) - 1)]


class Concurrency(Case):
    async def exercise(self, level: int, churn: bool = False):
        if not churn and level not in self.contract.settings.get("concurrency_levels", [10, 100, 1000]):
            self.skipTest(f"concurrency level {level} is not enabled in the contract")
        capacity = int(self.contract.settings.get("capacity", 10))
        if level > capacity:
            self.skipTest(f"{level} clients exceed declared test capacity {capacity}; no implicit downscaling")
        barrier = asyncio.Barrier(level)
        latencies, identities = [], []
        iterations = int(self.contract.settings.get("queries_per_client", 5))
        async def worker(index):
            client = await self.connect()
            await barrier.wait()
            for turn in range(iterations):
                if churn and turn:
                    client = await self.connect()
                token = f"{self.schema}_{index}_{turn}"
                started = time.monotonic()
                rows = await client.query("SELECT %s::text", (token,))
                latencies.append(time.monotonic() - started)
                self.assertEqual(rows, [(token,)], "response belongs to another client/query")
                if self.contract.features.get("named_prepared", False):
                    identities.append(await client.pid())
                if churn:
                    await client.close()
            await client.close()
        started = time.monotonic()
        tasks = [asyncio.create_task(worker(i)) for i in range(level)]
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), self.contract.number("recovery_timeout", 30))
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.wait(tasks, timeout=1)
        elapsed = time.monotonic() - started
        metrics = {"kind": "performance", "clients": level, "successes": len(latencies),
                   "elapsed_s": elapsed, "queries_per_second": len(latencies) / elapsed,
                   "p50_ms": percentile(latencies, .5) * 1000, "p95_ms": percentile(latencies, .95) * 1000,
                   "p99_ms": percentile(latencies, .99) * 1000, "backend_pids": sorted(set(identities))}
        self.evidence.append(metrics)
        budget = self.contract.settings.get("minimum_queries_per_second")
        if budget is not None:
            self.assertGreaterEqual(metrics["queries_per_second"], budget)

    async def test_concurrent_clients_10(self):
        await self.exercise(10)

    async def test_concurrent_clients_100(self):
        await self.exercise(100)

    async def test_concurrent_clients_1000(self):
        await self.exercise(1000)

    async def test_connection_churn(self):
        await self.exercise(min(10, int(self.contract.settings.get("capacity", 10))), churn=True)

    async def test_mixed_short_and_blocked_transactions(self):
        self.require("transactions")
        if int(self.contract.settings.get("pool_size", 2)) < 2:
            self.skipTest("mixed transactions need two backends")
        controller = await self.connect(direct=True)
        long_client, short_client = await self.connect(), await self.connect()
        key = int(self.schema[-12:], 16)
        await controller.query("BEGIN")
        await controller.query("SELECT pg_advisory_xact_lock(%s)", (key,))
        await long_client.query("BEGIN")
        pid = await long_client.pid()
        blocked = asyncio.create_task(long_client.query("SELECT pg_advisory_xact_lock(%s)", (key,)))
        async def waiting_on_lock():
            return bool((await self.direct.query("SELECT cardinality(pg_blocking_pids(%s))", (pid,)))[0][0])
        await self.eventually(waiting_on_lock, "long transaction waiting on our controlled advisory lock")
        self.assertEqual(await short_client.query("SELECT %s::text", (self.schema,)), [(self.schema,)])
        await controller.query("COMMIT")
        await blocked
        await long_client.query("COMMIT")

    async def test_concurrent_prepared_statements(self):
        self.require("named_prepared")
        count = min(10, int(self.contract.settings.get("capacity", 10)))
        barrier = asyncio.Barrier(count)
        async def worker(index):
            client = await self.connect(extended=True)
            await barrier.wait()
            try:
                await client.protocol(lambda pg: pg.send_prepare(b"concurrent", b"SELECT $1::integer", [23]))
                for value in range(3):
                    expected = index * 100 + value
                    results = await client.protocol(lambda pg: pg.send_query_prepared(b"concurrent", [str(expected).encode()]))
                    self.assertEqual(results[0].get_value(0, 0), str(expected).encode())
            finally:
                await client.close()
        tasks = [asyncio.create_task(worker(i)) for i in range(count)]
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), self.contract.number("recovery_timeout", 30))
        finally:
            for task in tasks:
                await self.stop_task(task)
