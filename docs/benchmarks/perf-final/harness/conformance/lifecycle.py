import asyncio
import os
import time

from .base import Case, capability


class Lifecycle(Case):
    @capability("cancel")
    async def test_cancel_query_and_reuse_connection(self):
        client = await self.connect()
        pid = await client.pid()
        task = asyncio.create_task(client.query("SELECT pg_sleep(%s)", (self.contract.number("operation_timeout", 10) * 3,)))
        async def running():
            rows = await self.direct.query("SELECT wait_event FROM pg_stat_activity WHERE pid=%s", (pid,))
            return rows and rows[0][0] == "PgSleep"
        await self.eventually(running, "backend executing the cancellable query")
        await asyncio.wait_for(client.connection.cancel_safe(), client.timeout)
        with self.assertRaises(Exception):
            await task
        self.assertEqual(await client.query("SELECT 44"), [(44,)])

    @capability("idle_client_timeout")
    async def test_idle_client_disconnected(self):
        client = await self.connect()
        await client.query("SELECT 1")
        await asyncio.sleep(self.contract.number("idle_client_timeout", 1) + self.contract.number("timing_tolerance", 0.5))
        with self.assertRaises(Exception):
            await client.query("SELECT 45")

    async def retired(self, setting: str):
        client = await self.connect()
        pid = await client.pid()
        await client.close()
        started = time.monotonic()
        async def gone():
            return not await self.direct.query("SELECT 1 FROM pg_stat_activity WHERE pid=%s", (pid,))
        await self.eventually(gone, f"server backend retired by {setting}")
        self.evidence.append({"retirement_seconds": time.monotonic() - started})

    @capability("idle_server_timeout")
    async def test_idle_server_retirement(self):
        await self.retired("idle_server_timeout")

    @capability("server_lifetime")
    async def test_server_lifetime(self):
        await self.retired("server_lifetime")

    async def admin(self):
        dsn = os.environ.get("POOLER_ADMIN_DSN")
        if not dsn:
            self.skipTest("optional POOLER_ADMIN_DSN is not configured")
        return await self.connect(dsn=dsn)

    @capability("admin_stats")
    async def test_documented_admin_statistics(self):
        client = await self.admin()
        query = self.contract.settings.get("stats_query")
        if not query:
            self.skipTest("documented stats_query is not configured")
        rows = await client.query(query)
        self.assertTrue(rows, "documented statistics query returned no records")
        self.evidence.append({"admin_stat_rows": len(rows)})

    @capability("reload")
    async def test_reload_preserves_query_service(self):
        await self.action("reload")
        client = await self.connect()
        self.assertEqual(await client.query("SELECT 46"), [(46,)])

    @capability("pause_resume")
    async def test_pause_queues_and_resume_releases(self):
        self.require_actions("pause", "resume")
        client = await self.connect()
        self.addAsyncCleanup(self.action, "resume")
        await self.action("pause")
        task = asyncio.create_task(client.query("SELECT 47"))
        await self.pending(task)
        await self.action("resume")
        self.assertEqual(await task, [(47,)])

    @capability("unsupported_config")
    async def test_invalid_configuration_rejected(self):
        pattern = self.contract.settings.get("invalid_config_error_pattern")
        if not pattern:
            self.skipTest("invalid_config_error_pattern must identify the documented rejection")
        argv = self.contract.actions.get("invalid_config")
        if not argv:
            self.skipTest("invalid_config adapter is not configured")
        if os.environ.get("CONFORMANCE_ALLOW_FAULTS") != "1":
            self.skipTest("configuration adapters require CONFORMANCE_ALLOW_FAULTS=1")
        process = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), self.contract.number("recovery_timeout", 15))
        except BaseException:
            process.kill()
            await process.wait()
            raise
        self.assertNotEqual(process.returncode, 0, "invalid configuration was unexpectedly accepted")
        message = (stdout + stderr).decode(errors="replace")
        self.assertRegex(message, pattern)

    @capability("drain")
    async def test_graceful_drain_contract(self):
        self.require("transactions")
        self.require_actions("drain", "pooler_start")
        client = await self.connect()
        await client.query("BEGIN")
        self.addAsyncCleanup(self.action, "pooler_start")
        await self.action("drain")
        self.assertEqual(await client.query("SELECT 51"), [(51,)])
        await client.query("COMMIT")
        await client.close()
        await self.action("pooler_start")
        replacement = await self.connect()
        self.assertEqual(await replacement.query("SELECT 52"), [(52,)])
