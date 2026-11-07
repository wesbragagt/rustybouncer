import os

import psycopg

from .base import Case, capability


class Security(Case):
    async def alternate(self, variable: str):
        dsn = os.environ.get(variable)
        if not dsn:
            self.skipTest(f"{variable} not provided; credentials/fixtures are environment-owned")
        return await self.connect(dsn=dsn)

    async def rejected(self, variable: str):
        dsn = os.environ.get(variable)
        if not dsn:
            self.skipTest(f"{variable} not provided")
        try:
            client = await self.connect(dsn=dsn)
        except psycopg.OperationalError as error:
            defaults = {"POOLER_BAD_AUTH_DSN": "password|authentication|no such user|not allowed",
                        "POOLER_DENIED_DSN": "database|permission denied|not allowed",
                        "POOLER_BAD_CERT_DSN": "certificate|SSL|TLS|hostname"}
            pattern = self.contract.settings.get(variable.lower() + "_error_pattern", defaults[variable])
            self.assertRegex(str(error), pattern, "connection failed for an unrelated reason, not the expected access rejection")
            self.evidence.append({"rejection_type": type(error).__name__, "sqlstate": getattr(error, "sqlstate", None)})
            return
        await client.close()
        self.fail(f"expected connection rejection for {variable}; connection succeeded")

    @capability("auth_rejection")
    async def test_invalid_credentials_rejected(self):
        await self.rejected("POOLER_BAD_AUTH_DSN")

    @capability("auth_rejection")
    async def test_unauthorized_database_rejected(self):
        await self.rejected("POOLER_DENIED_DSN")

    @capability("multi_user")
    async def test_separate_user_identity(self):
        other = await self.alternate("POOLER_ALT_USER_DSN")
        original = await self.connect()
        self.assertNotEqual(await original.query("SELECT current_user"), await other.query("SELECT current_user"))
        self.assertEqual(await other.query("SELECT 42"), [(42,)])

    @capability("multi_database")
    async def test_separate_database_identity(self):
        other = await self.alternate("POOLER_ALT_DATABASE_DSN")
        original = await self.connect()
        self.assertNotEqual(await original.query("SELECT current_database()"), await other.query("SELECT current_database()"))
        self.assertEqual(await other.query("SELECT 43"), [(43,)])

    @capability("tls")
    async def test_tls_negotiation(self):
        client = await self.connect(sslmode="require")
        self.assertTrue(client.connection.pgconn.ssl_in_use, "expected encrypted frontend connection")
        self.assertEqual(await client.query("SELECT 1"), [(1,)])

    @capability("tls_required")
    async def test_tls_required_rejects_plaintext(self):
        with self.assertRaises(Exception, msg="plaintext connection unexpectedly accepted"):
            await self.connect(sslmode="disable")

    @capability("certificate_validation")
    async def test_certificate_hostname_validation(self):
        valid = await self.alternate("POOLER_VERIFIED_TLS_DSN")
        self.assertTrue(valid.connection.pgconn.ssl_in_use)
        self.assertEqual(valid.connection.info.get_parameters().get("sslmode"), "verify-full", "verified TLS fixture must validate the server hostname and certificate")
        await self.rejected("POOLER_BAD_CERT_DSN")
