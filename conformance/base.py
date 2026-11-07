"""Per-test isolation and optional explicitly authorized external fault adapters."""
import asyncio
import functools
import os
import time
import unittest
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from psycopg import sql

from .client import Client
from .config import Contract


def capability(name: str):
    def decorate(method):
        @functools.wraps(method)
        async def wrapped(self, *args, **kwargs):
            self.require(name)
            return await method(self, *args, **kwargs)
        return wrapped
    return decorate


class Case(unittest.IsolatedAsyncioTestCase):
    contract: Contract
    pooler_dsn: str
    postgres_dsn: str

    async def asyncSetUp(self) -> None:
        self.evidence: list[dict[str, Any]] = []
        self.clients: list[Client] = []
        self.schema = "conformance_" + uuid.uuid4().hex
        self.direct = await self.connect(direct=True)
        await self.direct.query(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addAsyncCleanup(self.drop_schema)
        await self.direct.query(sql.SQL("CREATE TABLE {}.records (id integer PRIMARY KEY, value text NOT NULL)").format(sql.Identifier(self.schema)))
        self.table = sql.Identifier(self.schema, "records")

    async def drop_schema(self) -> None:
        # Use a fresh direct connection: fault adapters may have restarted PostgreSQL.
        for client in reversed(self.clients):
            await client.close()
        cleanup = await Client.connect(self.postgres_dsn, self.contract.number("operation_timeout", 10), self.evidence)
        try:
            await cleanup.query(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))
        finally:
            await cleanup.close()

    def require(self, name: str) -> None:
        if not self.contract.features.get(name, False):
            self.skipTest(f"capability {name!r} not declared by {self.contract.name}")

    async def connect(self, direct: bool = False, dsn: str | None = None, extended: bool = False, **kwargs: Any) -> Client:
        dsn = dsn or (self.postgres_dsn if direct else self.pooler_dsn)
        kwargs.setdefault("application_name", self.schema + ("_verification" if direct else ""))
        client = await Client.connect(dsn, self.contract.number("connect_timeout", 5), self.evidence, extended, **kwargs)
        client.timeout = self.contract.number("operation_timeout", 10)
        self.clients.append(client)
        self.addAsyncCleanup(client.close)
        return client

    async def eventually(self, predicate: Callable[[], Awaitable[Any]], description: str) -> Any:
        deadline = time.monotonic() + self.contract.number("recovery_timeout", 15)
        last = None
        while time.monotonic() < deadline:
            try:
                result = await predicate()
                if result:
                    return result
            except Exception as error:
                last = type(error).__name__
            await asyncio.sleep(self.contract.number("poll_interval", 0.05))
        self.fail(f"expected {description} before deadline; actual last error={last}; evidence={self.evidence[-8:]}")

    async def pending(self, task: asyncio.Task) -> None:
        done, _ = await asyncio.wait({task}, timeout=self.contract.number("observation_window", 0.2))
        if done:
            self.fail(f"expected queued operation; actual completed={task.result()!r}")
        self.addAsyncCleanup(self.stop_task, task)

    async def stop_task(self, task: asyncio.Task) -> None:
        if not task.done():
            task.cancel()
        await asyncio.wait({task}, timeout=1)
        if task.done() and not task.cancelled():
            task.exception()

    def require_actions(self, *names: str) -> None:
        if not all(self.contract.actions.get(name) for name in names):
            self.skipTest(f"optional adapter actions {names!r} are not configured")
        if os.environ.get("CONFORMANCE_ALLOW_FAULTS") != "1":
            self.skipTest("fault/admin adapter actions require CONFORMANCE_ALLOW_FAULTS=1")

    async def action(self, name: str) -> None:
        self.require_actions(name)
        argv = self.contract.actions[name]
        process = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), self.contract.number("recovery_timeout", 15))
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        self.assertEqual(process.returncode, 0, f"adapter {name} failed: {stderr.decode(errors='replace')[:500]}")
        self.evidence.append({"adapter": name, "output": stdout.decode(errors="replace")[:500]})
