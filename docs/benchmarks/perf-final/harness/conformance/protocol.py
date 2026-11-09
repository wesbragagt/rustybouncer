import asyncio

import psycopg

from .base import Case, capability


class Protocol(Case):
    async def test_simple_query_error_recovery(self):
        client = await self.connect()
        with self.assertRaises(psycopg.errors.DivisionByZero):
            await client.query("SELECT 1/0")
        self.assertEqual(await client.query("SELECT 42"), [(42,)])

    async def test_simple_null_binary_and_parameter_decoding(self):
        client = await self.connect()
        self.assertEqual(await client.query("SELECT %s::integer, %s::text, %s::bytea", (3, None, b"\x00\xff")), [(3, None, b"\x00\xff")])

    @capability("extended")
    async def test_extended_parse_bind_execute_sync(self):
        client = await self.connect(extended=True)
        for value in (1, 5, 23):
            self.assertEqual(await client.query("SELECT %s::integer + 1, %s::text", (value, None)), [(value + 1, None)])
        with self.assertRaises(psycopg.errors.DivisionByZero):
            await client.query("SELECT 1/%s::integer", (0,))
        self.assertEqual(await client.query("SELECT %s::integer", (9,)), [(9,)])

    @capability("named_prepared")
    async def test_named_protocol_statements_across_transactions(self):
        self.require("transactions")
        client = await self.connect(extended=True)
        await client.protocol(lambda pg: pg.send_prepare(b"same_name", b"SELECT $1::integer + 1", [23]))
        for value in (1, 10, 100):
            await client.query("BEGIN")
            results = await client.protocol(lambda pg: pg.send_query_prepared(b"same_name", [str(value).encode()]))
            self.assertEqual(results[0].get_value(0, 0), str(value + 1).encode())
            await client.query("COMMIT")

    @capability("named_prepared")
    async def test_same_statement_name_isolated_between_clients(self):
        a, b = await self.connect(extended=True), await self.connect(extended=True)
        await a.protocol(lambda pg: pg.send_prepare(b"collision", b"SELECT 111"))
        await b.protocol(lambda pg: pg.send_prepare(b"collision", b"SELECT 222"))
        for client, expected in ((a, b"111"), (b, b"222")):
            result = await client.protocol(lambda pg: pg.send_query_prepared(b"collision", []))
            self.assertEqual(result[0].get_value(0, 0), expected)

    @capability("extended")
    async def test_unnamed_binary_parameters_and_results(self):
        client = await self.connect(extended=True)
        await client.protocol(lambda pg: pg.send_prepare(b"", b"SELECT $1::integer", [23]))
        results = await client.protocol(lambda pg: pg.send_query_prepared(b"", [b"\x00\x00\x00\x2a"], param_formats=[1], result_format=1))
        self.assertEqual(results[0].get_value(0, 0), b"\x00\x00\x00\x2a")

    @capability("sql_prepare")
    async def test_sql_prepare_is_separate_from_protocol_prepare(self):
        client = await self.connect()
        await client.query("PREPARE conformance_sql(integer) AS SELECT $1 + 2")
        self.assertEqual(await client.query("EXECUTE conformance_sql(5)"), [(7,)])
        await client.query("DEALLOCATE conformance_sql")

    @capability("pipeline")
    async def test_pipeline_preserves_result_order(self):
        client = await self.connect(extended=True)
        async def pipeline():
            async with client.connection.pipeline():
                cursors = [await client.connection.execute("SELECT %s::integer", (i,)) for i in range(12)]
            return [await cursor.fetchall() for cursor in cursors]
        self.assertEqual(await asyncio.wait_for(pipeline(), client.timeout), [[(i,)] for i in range(12)])

    @capability("prepared_eviction")
    async def test_prepared_statements_survive_declared_capacity(self):
        client = await self.connect(extended=True)
        count = int(self.contract.settings["prepared_capacity"]) + 1
        for index in range(count):
            name = f"statement_{index}".encode()
            await client.protocol(lambda pg, name=name: pg.send_prepare(name, f"SELECT $1::integer + {index}".encode(), [23]))
            result = await client.protocol(lambda pg, name=name: pg.send_query_prepared(name, [b"7"]))
            self.assertEqual(result[0].get_value(0, 0), str(7 + index).encode())
        result = await client.protocol(lambda pg: pg.send_query_prepared(b"statement_0", [b"9"]))
        self.assertEqual(result[0].get_value(0, 0), b"9")

    @capability("named_prepared")
    async def test_named_statement_after_forced_backend_reassignment(self):
        self.require("release_after_transaction")
        count = int(self.contract.settings["pool_size"])
        if count < 2:
            self.skipTest("forced reassignment needs two server connections")
        client = await self.connect(extended=True)
        await client.query("BEGIN")
        original = await client.pid()
        await client.protocol(lambda pg: pg.send_prepare(b"move_me", b"SELECT $1::integer + 7", [23]))
        await client.query("COMMIT")
        holders = [await self.connect() for _ in range(count)]
        assigned = []
        for holder in holders:
            await holder.query("BEGIN")
            assigned.append(await holder.pid())
        alternative = next(i for i, pid in enumerate(assigned) if pid != original)
        await holders[alternative].query("ROLLBACK")
        await client.query("BEGIN")
        self.assertNotEqual(await client.pid(), original)
        results = await client.protocol(lambda pg: pg.send_query_prepared(b"move_me", [b"5"]))
        self.assertEqual(results[0].get_value(0, 0), b"12")
        await client.query("COMMIT")
        for i, holder in enumerate(holders):
            if i != alternative:
                await holder.query("ROLLBACK")
