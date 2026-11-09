#!/usr/bin/env python3
"""Real PostgreSQL failover and CTE tests for the opt-in simple-query router.

Run: cargo build --locked && python3 tests/failover.py
Requires Docker, Python 3 and postgres:16; no Python packages are required.
Each test creates an isolated primary + two streaming replicas and cleans them up.
"""
import concurrent.futures
import contextlib
import os
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get("RUSTYBOUNCER_POSTGRES_IMAGE", "postgres:16")
BINARY = Path(os.environ.get("RUSTYBOUNCER_BIN", ROOT / "target/debug/rustybouncer"))
WAIT_SECONDS = 4


def docker(*args):
    try:
        return subprocess.check_output(
            ["docker", *args], text=True, stderr=subprocess.STDOUT, timeout=30
        ).strip()
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"docker {' '.join(args[:3])}: {exc.output}") from exc


def eventually(fn, seconds=30, description="condition"):
    deadline, last = time.monotonic() + seconds, None
    while time.monotonic() < deadline:
        try:
            result = fn()
            if result:
                return result
        except (OSError, RuntimeError) as exc:
            last = exc
        time.sleep(0.05)
    raise AssertionError(f"{description} did not become true in {seconds}s: {last}")


def sql(node, query):
    return docker("exec", node, "psql", "-h", "127.0.0.1", "-U", "postgres", "-At", "-c", query)


def port(node):
    return int(docker("port", node, "5432/tcp").rsplit(":", 1)[1])


class PgError(RuntimeError):
    def __init__(self, body):
        fields = {}
        for field in body.rstrip(b"\0").split(b"\0"):
            if field:
                fields[chr(field[0])] = field[1:].decode(errors="replace")
        self.code = fields.get("C", "")
        self.severity = fields.get("S", "")
        super().__init__(f"{self.code}: {fields.get('M', repr(body))}")


class Pg:
    """Minimal simple-query client; no retries, so continuity is the proxy's work."""
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=WAIT_SECONDS + 5)
        params = b"user\0postgres\0database\0postgres\0\0"
        self.sock.sendall(struct.pack("!II", len(params) + 8, 196608) + params)
        try:
            self.read_cycle()
        except BaseException:
            self.close()
            raise

    def exact(self, n):
        data = b""
        while len(data) < n:
            more = self.sock.recv(n - len(data))
            if not more:
                raise OSError("connection closed")
            data += more
        return data

    def read_cycle(self):
        rows, error = [], None
        while True:
            kind = self.exact(1)
            length = struct.unpack("!I", self.exact(4))[0]
            if not 4 <= length <= 16 * 1024 * 1024:
                raise RuntimeError(f"invalid PostgreSQL frame size {length}")
            body = self.exact(length - 4)
            if kind == b"K":
                self.cancel_key = struct.unpack("!II", body)
            if kind == b"E":
                error = PgError(body)
                if error.severity in ("FATAL", "PANIC"):
                    raise error
            if kind == b"D":
                count = struct.unpack("!H", body[:2])[0]
                offset, row = 2, []
                for _ in range(count):
                    size = struct.unpack("!i", body[offset:offset + 4])[0]
                    offset += 4
                    row.append(None if size < 0 else body[offset:offset + size].decode())
                    offset += max(size, 0)
                rows.append(row)
            if kind == b"Z":
                self.transaction_status = body
                if error:
                    raise error
                return rows

    def send_query(self, query):
        body = query.encode() + b"\0"
        self.sock.sendall(b"Q" + struct.pack("!I", len(body) + 4) + body)

    def query(self, query):
        self.send_query(query)
        return self.read_cycle()

    def close(self):
        self.sock.close()


class Cluster:
    def __init__(self):
        self.prefix = "rustybouncer-test-" + uuid.uuid4().hex[:8]
        self.nodes = [self.prefix + "-" + name for name in ("a", "b", "c")]
        self.process = None
        self.directory = tempfile.TemporaryDirectory()
        self.log_path = Path(self.directory.name) / "proxy.log"
        self.log = None
        self.network_created = False
        self.paused = set()

    def start(self):
        docker("network", "create", self.prefix)
        self.network_created = True
        primary = self.nodes[0]
        docker("run", "-d", "--name", primary, "--network", self.prefix,
               "-p", "127.0.0.1::5432", "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
               IMAGE, "-c", "wal_level=replica", "-c", "max_wal_senders=10",
               "-c", "cluster_name=a")
        eventually(lambda: sql(primary, "SELECT 1") == "1", description="primary startup")
        docker("exec", primary, "bash", "-c",
               "echo 'host replication all 0.0.0.0/0 trust' >> \"$PGDATA/pg_hba.conf\"")
        sql(primary, "SELECT pg_reload_conf()")
        sql(primary, "CREATE TABLE continuity (id integer PRIMARY KEY, payload text NOT NULL)")
        for node, name in zip(self.nodes[1:], ("b", "c")):
            docker("run", "-d", "--name", node, "--network", self.prefix,
                   "-p", "127.0.0.1::5432", "--user", "postgres", IMAGE, "bash", "-c",
                   f"pg_basebackup -h {primary} -U postgres -D /tmp/standby -R -X stream && "
                   f"exec postgres -D /tmp/standby -c listen_addresses='*' -c cluster_name={name}")
        for replica in self.nodes[1:]:
            eventually(lambda replica=replica: sql(replica, "SELECT pg_is_in_recovery()") == "t",
                       description="replica startup")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.proxy_port = sock.getsockname()[1]
        config = Path(self.directory.name) / "config.toml"
        config.write_text(f'''listen = "127.0.0.1:{self.proxy_port}"
max_clients = 64
probe_interval_ms = 100
probe_timeout_ms = 300
wait_timeout_ms = {WAIT_SECONDS * 1000}
startup_timeout_ms = 2000
probe_user = "postgres"
probe_database = "postgres"
route_selects = true
''' + "".join(f'''[[nodes]]
name = "{name}"
host = "127.0.0.1"
port = {port(node)}
''' for node, name in zip(self.nodes, ("a", "b", "c"))))
        self.log = self.log_path.open("w")
        self.process = subprocess.Popen([str(BINARY), str(config)], stdout=self.log, stderr=self.log)
        eventually(self.ready, description="proxy startup")
        # Both replicas must have entered discovery before testing round robin.
        eventually(self.replicas_discovered, description="replica discovery")

    def ready(self):
        if self.process is None:
            return False
        if self.process.poll() is not None:
            raise AssertionError(f"proxy exited: {self.log_path.read_text()}")
        with contextlib.closing(Pg(self.proxy_port)) as client:
            return client.query("SELECT 1") == [["1"]]

    def replicas_discovered(self):
        with contextlib.closing(Pg(self.proxy_port)) as client:
            return set(self.identity(client) for _ in range(4)) == {"b", "c"}

    def connect(self):
        return Pg(self.proxy_port)

    @staticmethod
    def identity(client):
        return client.query("SELECT current_setting('cluster_name')")[0][0]

    def pause(self, index):
        docker("pause", self.nodes[index])
        self.paused.add(index)

    def unpause(self, index):
        docker("unpause", self.nodes[index])
        self.paused.discard(index)

    def promote(self, index=1):
        self.ensure_replica_ready(index)
        sql(self.nodes[index], "SELECT pg_promote(true, 10)")
        eventually(lambda: sql(self.nodes[index], "SELECT pg_is_in_recovery()") == "f",
                   description="replica promotion")

    def ensure_replica_ready(self, index):
        # Visibility/catch-up checks are explicit in the tests that need them.
        # Promotion itself waits for recovery to complete locally.
        eventually(lambda: sql(self.nodes[index], "SELECT 1") == "1")

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        if self.log:
            self.log.close()
        for index in list(self.paused):
            with contextlib.suppress(RuntimeError):
                self.unpause(index)
        for node in self.nodes:
            with contextlib.suppress(RuntimeError):
                docker("rm", "-fv", node)
        if self.network_created:
            with contextlib.suppress(RuntimeError):
                docker("network", "rm", self.prefix)
        self.directory.cleanup()


class FailoverContinuity(unittest.TestCase):
    def setUp(self):
        self.cluster = Cluster()
        self.addCleanup(self.cluster.close)
        try:
            self.cluster.start()
        except BaseException:
            self.print_diagnostics()
            raise

    def tearDown(self):
        # Keep failure diagnostics before temporary files and containers disappear.
        result = getattr(getattr(self, "_outcome", None), "result", None)
        if result and any(test is self for test, _ in result.failures + result.errors):
            self.print_diagnostics()

    def print_diagnostics(self):
        if self.cluster.log_path.exists():
            print("\n--- rustybouncer log ---\n" + self.cluster.log_path.read_text(), flush=True)
        for node in self.cluster.nodes:
            with contextlib.suppress(RuntimeError):
                print(f"\n--- {node} log ---\n" + docker("logs", "--tail", "12", node), flush=True)

    def client(self):
        client = self.cluster.connect()
        self.addCleanup(client.close)
        return client

    def test_01_same_connection_reads_round_robin_through_primary_failover(self):
        """No client retries/reconnects: reads continue throughout leader loss."""
        client = self.client()
        identities = [self.cluster.identity(client) for _ in range(6)]
        self.assertEqual(set(identities), {"b", "c"})
        self.assertTrue(all(a != b for a, b in zip(identities, identities[1:])), identities)
        stop, samples, errors = threading.Event(), [], []
        def read_loop():
            while not stop.is_set():
                try:
                    samples.append((time.monotonic(), self.cluster.identity(client)))
                except BaseException as exc:
                    errors.append(exc)
                    return
                stop.wait(0.02)
        worker = threading.Thread(target=read_loop)
        worker.start()
        try:
            eventually(lambda: len(samples) >= 3)
            self.cluster.pause(0)
            eventually(lambda: "primary: None" in self.cluster.log_path.read_text(),
                       description="primary discovery withdrawal")
            no_primary_at = time.monotonic()
            eventually(lambda: sum(stamp > no_primary_at for stamp, _ in samples) >= 3,
                       description="reads served after primary was withdrawn")
            self.cluster.promote()
            promoted_at = time.monotonic()
            eventually(lambda: any(stamp > promoted_at for stamp, _ in samples))
        finally:
            stop.set()
            worker.join(timeout=10)
        self.assertFalse(worker.is_alive(), "read worker did not stop")
        self.assertEqual(errors, [], f"SELECT failed during failover: {errors}")
        self.assertTrue(set(identity for _, identity in samples) <= {"b", "c"})

    def test_02_unsent_write_waits_then_continues_on_same_connection(self):
        """An autocommit statement received during the leader gap waits safely."""
        client = self.client()
        client.query("INSERT INTO continuity VALUES (1, 'before')")
        eventually(lambda: sql(self.cluster.nodes[1], "SELECT count(*) FROM continuity") == "1")
        self.cluster.pause(0)
        time.sleep(0.7)  # Let discovery withdraw the paused primary.
        with concurrent.futures.ThreadPoolExecutor() as executor:
            waiting = executor.submit(client.query, "INSERT INTO continuity VALUES (2, 'after') RETURNING id")
            time.sleep(0.4)
            self.assertFalse(waiting.done(), "write did not wait for primary promotion")
            self.cluster.promote()
            self.assertEqual(waiting.result(timeout=WAIT_SECONDS + 3), [["2"]])
        self.assertEqual(sql(self.cluster.nodes[1], "SELECT id FROM continuity ORDER BY id"), "1\n2")
        # Still the exact same client socket; writes after recovery also succeed.
        self.assertEqual(client.query("INSERT INTO continuity VALUES (3, 'next') RETURNING id"), [["3"]])

    def test_03_concurrent_writes_recover_and_split_brain_is_refused(self):
        """Concurrent queued clients resume once; ambiguous leaders cannot accept writes."""
        clients = [self.client() for _ in range(6)]
        self.cluster.pause(0)
        time.sleep(0.7)
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(clients)) as executor:
            futures = [executor.submit(client.query,
                        f"INSERT INTO continuity VALUES ({index}, 'concurrent') RETURNING id")
                       for index, client in enumerate(clients, 1)]
            time.sleep(0.3)
            self.assertTrue(all(not future.done() for future in futures))
            self.cluster.promote()
            for index, future in enumerate(futures, 1):
                self.assertEqual(future.result(timeout=WAIT_SECONDS + 3), [[str(index)]])
        self.assertEqual(sql(self.cluster.nodes[1], "SELECT count(*) FROM continuity"), "6")
        self.cluster.unpause(0)
        eventually(lambda: "multiple writable" in self.cluster.log_path.read_text(),
                   description="split-brain detection")
        started = time.monotonic()
        with self.assertRaises((PgError, OSError)):
            clients[0].query("INSERT INTO continuity VALUES (99, 'must not execute')")
        self.assertLess(time.monotonic() - started, WAIT_SECONDS + 3)
        for index in (0, 1):
            self.assertEqual(sql(self.cluster.nodes[index], "SELECT count(*) FROM continuity WHERE id=99"), "0")

    def test_04_interrupted_transaction_fails_without_replay(self):
        """Transactions cannot migrate: rollback and uncertain outcomes stay explicit."""
        client = self.client()
        client.query("INSERT INTO continuity VALUES (1, 'committed')")
        eventually(lambda: sql(self.cluster.nodes[1], "SELECT count(*) FROM continuity") == "1")
        client.query("BEGIN")
        self.assertEqual(self.cluster.identity(client), "a", "transaction SELECT escaped the primary")
        client.query("INSERT INTO continuity VALUES (99, 'uncommitted')")
        self.cluster.pause(0)
        self.cluster.promote()
        with self.assertRaises((PgError, OSError)):
            client.query("COMMIT")
        # Wait for the replacement to enter discovery before creating new writes.
        recovered = self.client()
        self.assertEqual(recovered.query("INSERT INTO continuity VALUES (2, 'reconnected') RETURNING id"), [["2"]])
        self.assertEqual(sql(self.cluster.nodes[1], "SELECT id FROM continuity ORDER BY id"), "1\n2")
        self.assertEqual(sql(self.cluster.nodes[1], "SELECT count(*) FROM continuity WHERE id=99"), "0")

    def test_05_failed_replica_leaves_rotation_without_disrupting_reads(self):
        """A replica failure is removed; same-client reads survive primary loss too."""
        client = self.client()
        self.assertEqual(set(self.cluster.identity(client) for _ in range(4)), {"b", "c"})
        self.cluster.pause(2)
        # The first read may hit the now-dead replica before health checks notice;
        # pure SELECT retries must occur inside the router, never in this client.
        identities = [self.cluster.identity(client) for _ in range(8)]
        self.assertEqual(set(identities), {"b"})
        self.cluster.pause(0)
        self.assertEqual([self.cluster.identity(client) for _ in range(4)], ["b"] * 4)
        self.cluster.promote()
        # With no remaining replica, the promoted primary is the read fallback.
        time.sleep(0.5)
        self.assertEqual([self.cluster.identity(client) for _ in range(4)], ["b"] * 4)
        self.assertEqual(client.query("INSERT INTO continuity VALUES (1, 'fallback') RETURNING id"), [["1"]])

    def test_06_read_only_ctes_round_robin_and_continue_through_failover(self):
        client = self.client()
        queries = [
            "WITH node AS (SELECT current_setting('cluster_name') AS name) SELECT name FROM node",
            "WITH node(name) AS MATERIALIZED (SELECT current_setting('cluster_name')) SELECT name FROM node",
            "WITH node AS NOT MATERIALIZED (WITH nested AS (SELECT current_setting('cluster_name') AS name) SELECT name FROM nested) SELECT name FROM node",
            "WITH RECURSIVE numbers(n) AS (VALUES (1) UNION ALL SELECT n+1 FROM numbers WHERE n<3) SELECT current_setting('cluster_name') FROM numbers WHERE n=3",
        ]
        identities = [client.query(query)[0][0] for query in queries * 2]
        self.assertEqual(set(identities), {"b", "c"})
        self.assertTrue(all(a != b for a, b in zip(identities, identities[1:])), identities)
        self.cluster.pause(0)
        eventually(lambda: "primary: None" in self.cluster.log_path.read_text(),
                   description="primary withdrawal before CTE reads")
        self.assertEqual(set(client.query(query)[0][0] for query in queries), {"b", "c"})
        self.cluster.promote()
        eventually(lambda: "primary: Some(1), replicas: [2]" in self.cluster.log_path.read_text(),
                   description="replica promotion discovery")
        self.assertEqual([client.query(query)[0][0] for query in queries], ["c"] * 4)
        client.query("BEGIN")
        self.assertEqual(client.query(queries[0]), [["b"]])
        client.query("ROLLBACK")

    def test_07_data_modifying_ctes_execute_on_primary(self):
        queries = [
            "WITH changed AS (INSERT INTO continuity VALUES (1, 'cte') RETURNING id) SELECT current_setting('cluster_name') FROM changed",
            "WITH changed AS (UPDATE continuity SET payload='updated' WHERE id=1 RETURNING id) SELECT current_setting('cluster_name') FROM changed",
            "WITH changed AS (DELETE FROM continuity WHERE id=1 RETURNING id) SELECT current_setting('cluster_name') FROM changed",
            "WITH seed AS (SELECT 2 AS id) INSERT INTO continuity SELECT id, 'outer write' FROM seed RETURNING current_setting('cluster_name')",
            "WITH unreferenced AS (INSERT INTO continuity VALUES (3, 'hidden')) SELECT current_setting('cluster_name')",
            "WITH locked AS (SELECT * FROM continuity WHERE id=2 FOR SHARE) SELECT current_setting('cluster_name') FROM locked",
        ]
        for query in queries:
            with self.subTest(query=query):
                # A fresh session prevents prior primary pinning from masking classification bugs.
                client = self.client()
                self.assertEqual(client.query(query), [["a"]])
                client.close()
        self.assertEqual(sql(self.cluster.nodes[0], "SELECT id FROM continuity ORDER BY id"), "2\n3")


if __name__ == "__main__":
    unittest.main(verbosity=2)
