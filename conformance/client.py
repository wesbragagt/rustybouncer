"""Bounded asynchronous libpq clients; queries are never silently retried."""
import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any

import psycopg
from psycopg import AsyncClientCursor, AsyncConnection, AsyncCursor, pq


async def bounded(operation: Awaitable[Any], seconds: float, abort: Callable[[], None]) -> Any:
    task = asyncio.ensure_future(operation)
    try:
        done, _ = await asyncio.wait({task}, timeout=seconds)
        if task in done:
            return task.result()
        raise TimeoutError(f"operation did not complete within {seconds:g}s; connection discarded")
    finally:
        if not task.done():
            abort()
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=1)
            if not done:
                raise RuntimeError("driver task did not stop after connection disposal")
        if task.done() and not task.cancelled():
            task.exception()  # Retrieve failures even when the caller was cancelled.



class Client:
    def __init__(self, connection: AsyncConnection[Any], timeout: float, evidence: list):
        self.connection = connection
        self.timeout = timeout
        self.evidence = evidence

    @classmethod
    async def connect(cls, dsn: str, timeout: float, evidence: list, extended: bool = False, **kwargs: Any) -> "Client":
        kwargs.setdefault("client_encoding", "UTF8")
        factory = AsyncCursor if extended else AsyncClientCursor
        connection = await asyncio.wait_for(AsyncConnection.connect(
            dsn, autocommit=True, cursor_factory=factory, prepare_threshold=None,
            connect_timeout=max(1, math.ceil(timeout)), **kwargs,
        ), timeout + 1)
        return cls(connection, timeout, evidence)

    def abort(self) -> None:
        self.connection.pgconn.finish()

    async def close(self) -> None:
        await self.connection.close()

    async def query(self, query: Any, params: Any = None) -> list:
        started = time.monotonic()
        async def execute() -> list:
            async with self.connection.cursor() as cursor:
                await cursor.execute(query, params)
                return await cursor.fetchall() if cursor.description else []
        try:
            rows = await bounded(execute(), self.timeout, self.abort)
            self.evidence.append({"operation": "query", "duration_s": time.monotonic() - started, "rows": len(rows)})
            return rows
        except Exception as error:
            self.evidence.append({"operation": "query", "duration_s": time.monotonic() - started,
                                  "error_type": type(error).__name__, "sqlstate": getattr(error, "sqlstate", None)})
            raise

    async def pid(self) -> int:
        row = (await self.query("SELECT pg_backend_pid(), inet_server_addr()::text, inet_server_port()"))[0]
        self.evidence.append({"backend_pid": row[0], "backend_address": row[1], "backend_port": row[2]})
        return row[0]

    async def protocol(self, send: Callable[[Any], None]) -> list:
        async def collect() -> list:
            connection = self.connection.pgconn
            send(connection)
            while connection.flush() == 1:
                await ready(connection.socket, write=True)
            results = []
            while True:
                connection.consume_input()
                if connection.is_busy():
                    await ready(connection.socket)
                    continue
                result = connection.get_result()
                if result is None:
                    return results
                if result.status == pq.ExecStatus.FATAL_ERROR:
                    raise psycopg.ProgrammingError(result.error_message.decode(errors="replace"))
                results.append(result)
        return await bounded(collect(), self.timeout, self.abort)


async def ready(fd: int, write: bool = False) -> None:
    loop = asyncio.get_running_loop()
    event = loop.create_future()
    register = loop.add_writer if write else loop.add_reader
    remove = loop.remove_writer if write else loop.remove_reader
    def wake() -> None:
        if not event.done():
            event.set_result(None)
    register(fd, wake)
    try:
        await event
    finally:
        remove(fd)
