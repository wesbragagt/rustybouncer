"""Bounded asynchronous libpq clients; queries are never silently retried."""
import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any

import psycopg
from psycopg import AsyncClientCursor, AsyncConnection, AsyncCursor, pq


def positive_seconds(seconds: float) -> None:
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("deadline must be finite and positive")


async def stop_owned(task: asyncio.Future, seconds: float = 1) -> None:
    """Cancel and join before disposing sockets (readiness registrations own fds)."""
    if not task.done():
        task.cancel()
    done, _ = await asyncio.wait({task}, timeout=seconds)
    if not done:
        task.add_done_callback(lambda future: None if future.cancelled() else future.exception())
        raise RuntimeError("owned task ignored cancellation; Python cannot forcibly stop it")
    if not task.cancelled():
        task.exception()


async def bounded(operation: Awaitable[Any], seconds: float, abort: Callable[[], None]) -> Any:
    positive_seconds(seconds)
    task = asyncio.ensure_future(operation)
    try:
        done, _ = await asyncio.wait({task}, timeout=seconds)
        if task not in done:
            raise TimeoutError(f"operation did not complete within {seconds:g}s; connection discarded")
    except BaseException as primary:
        try:
            await stop_owned(task)
            abort()
        except BaseException as cleanup:
            if not task.done():
                task.add_done_callback(lambda _: abort())
            raise BaseExceptionGroup("operation and disposal failed", [primary, cleanup]) from None
        raise
    return task.result()


class Client:
    def __init__(self, connection: AsyncConnection[Any], timeout: float, evidence: list):
        positive_seconds(timeout)
        self.connection = connection
        self.timeout = timeout
        self.cleanup_timeout = timeout
        self.evidence = evidence

    @classmethod
    async def connect(cls, dsn: str, timeout: float, evidence: list, extended: bool = False, **kwargs: Any) -> "Client":
        kwargs.setdefault("client_encoding", "UTF8")
        factory = AsyncCursor if extended else AsyncClientCursor
        positive_seconds(timeout)
        acquired: list[AsyncConnection[Any]] = []
        async def acquire() -> AsyncConnection[Any]:
            connection = await AsyncConnection.connect(
                dsn, autocommit=True, cursor_factory=factory, prepare_threshold=None,
                connect_timeout=max(1, math.ceil(timeout)), **kwargs,
            )
            acquired.append(connection)
            return connection
        def discard() -> None:
            for connection in acquired:
                connection.pgconn.finish()
        connection = await bounded(acquire(), timeout, discard)
        return cls(connection, timeout, evidence)

    def abort(self) -> None:
        self.connection.pgconn.finish()

    async def close(self) -> None:
        await bounded(self.connection.close(), self.cleanup_timeout, self.abort)

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
