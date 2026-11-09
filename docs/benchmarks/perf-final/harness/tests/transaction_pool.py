#!/usr/bin/env python3
"""PERF-008 slice 1: real transaction state, release, isolation and cancellation.

Run the whole file. Extended-protocol and full queue/capacity acceptance are
subsequent required slices, never claimed by this partial milestone suite.
"""
import concurrent.futures
import io
import json
import os
import unittest
from pathlib import Path

import failover as f
from pool_support import PoolCase


class Transactions(PoolCase):
    evidence_env = 'TRANSACTION_POOL_EVIDENCE'
    pool_mode = 'transaction'

    def assert_pool_free(self):
        # All physical capacity must be available to other frontends even while
        # the original frontend sockets stay connected. PID rotation alone is
        # not required: the same idle backend may legitimately be reused.
        holders = [self.client() for _ in range(2)]
        pids = []
        for holder in holders:
            holder.query('BEGIN')
            pids.append(self.pid(holder))
        self.assertEqual(len(set(pids)), 2)
        for holder in holders:
            holder.query('ROLLBACK')
            holder.close()

    def test_pin_commit_visibility_and_capacity_release(self):
        a = self.client()
        a.query('BEGIN ISOLATION LEVEL READ COMMITTED')
        original = self.pid(a)
        a.query("INSERT INTO pool_data VALUES (1,'committed')")
        self.assertEqual(self.pid(a), original)
        self.assertEqual(f.sql(self.fixture.nodes[0], 'SELECT count(*) FROM pool_data'), '0')
        a.query('COMMIT')
        self.assertEqual(a.transaction_status, b'I')
        self.assertEqual(f.sql(self.fixture.nodes[0], 'SELECT value FROM pool_data WHERE id=1'), 'committed')
        self.assert_pool_free()
        self.assertEqual(a.query('SELECT 41'), [['41']])

    def test_rollback_discards_writes_and_releases_capacity(self):
        a = self.client()
        a.query('BEGIN')
        original = self.pid(a)
        a.query("INSERT INTO pool_data VALUES (2,'rolled-back')")
        self.assertEqual(self.pid(a), original)
        a.query('ROLLBACK')
        self.assertEqual(f.sql(self.fixture.nodes[0], 'SELECT count(*) FROM pool_data'), '0')
        self.assert_pool_free()

    def test_exclusive_concurrent_transactions_and_local_state(self):
        clients = [self.client(), self.client()]
        pids = []
        for index, client in enumerate(clients):
            client.query('BEGIN')
            client.query(f"SET LOCAL application_name='transaction-{index}'")
            pids.append(self.pid(client))
        self.assertEqual(len(set(pids)), 2)
        self.assertEqual([client.query('SHOW application_name') for client in clients],
                         [[['transaction-0']], [['transaction-1']]])
        for client in clients:
            client.query('COMMIT')
        self.assert_pool_free()
        for client in clients:
            self.assertEqual(client.query('SHOW application_name'), [['']])

    def test_savepoint_error_abort_and_recovery_keep_same_backend(self):
        a = self.client()
        a.query('BEGIN')
        original = self.pid(a)
        a.query("INSERT INTO pool_data VALUES (3,'retained')")
        a.query('SAVEPOINT checkpoint')
        with self.assertRaises(f.PgError) as error:
            a.query("INSERT INTO pool_data VALUES (3,'duplicate')")
        self.assertEqual(error.exception.code, '23505')
        self.assertEqual(a.transaction_status, b'E')
        with self.assertRaises(f.PgError) as error:
            a.query('SELECT 1')
        self.assertEqual(error.exception.code, '25P02')
        a.query('ROLLBACK TO SAVEPOINT checkpoint')
        self.assertEqual(a.transaction_status, b'T')
        self.assertEqual(self.pid(a), original)
        a.query("INSERT INTO pool_data VALUES (4,'second')")
        a.query('RELEASE SAVEPOINT checkpoint')
        a.query('COMMIT')
        self.assertEqual(f.sql(self.fixture.nodes[0], 'SELECT count(*) FROM pool_data'), '2')
        self.assert_pool_free()

    def test_aborted_transaction_cannot_donate_backend_until_rollback(self):
        a, b, waiting = self.client(), self.client(), self.client()
        a.query('BEGIN')
        first = self.pid(a)
        with self.assertRaises(f.PgError):
            a.query('SELECT 1/0')
        b.query('BEGIN')
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(waiting.query, 'SELECT 43')
            f.eventually(lambda: self.scrape().value('queries_in_flight') == 1)
            self.assertFalse(future.done())
            a.query('ROLLBACK')
            self.assertEqual(future.result(timeout=5), [['43']])
        b.query('ROLLBACK')
        self.assertGreater(first, 0)
        self.assert_pool_free()

    def test_disconnect_with_open_or_aborted_transaction_rolls_back(self):
        for index, abort in enumerate((False, True)):
            a = self.client()
            a.query('BEGIN')
            a.query(f"INSERT INTO pool_data VALUES ({index},'not-committed')")
            if abort:
                with self.assertRaises(f.PgError):
                    a.query('SELECT 1/0')
            a.close()
            f.eventually(lambda: self.scrape().value('clients_active') == 0)
            self.assertEqual(f.sql(self.fixture.nodes[0], 'SELECT count(*) FROM pool_data'), '0')
            self.assert_pool_free()

    def test_autocommit_errors_release_and_next_queries_remain_correct(self):
        a = self.client()
        with self.assertRaises(f.PgError) as error:
            a.query('SELECT 1/0')
        self.assertEqual(error.exception.code, '22012')
        self.assertEqual(a.transaction_status, b'I')
        self.assert_pool_free()
        self.assertEqual(a.query('SELECT 44'), [['44']])

    def test_commit_and_rollback_chain_retain_actual_nonidle_status(self):
        a = self.client()
        a.query('BEGIN')
        first = self.pid(a)
        for command in ('COMMIT AND CHAIN', 'ROLLBACK AND CHAIN'):
            a.query(command)
            self.assertEqual(a.transaction_status, b'T')
            self.assertEqual(self.pid(a), first)
        a.query('ROLLBACK')
        self.assert_pool_free()

    def test_stateless_primary_observations_do_not_permanently_pin(self):
        a = self.client()
        self.assertGreater(self.pid(a), 0)
        self.assertEqual(a.query('SELECT current_user,current_database()'), [['postgres', 'postgres']])
        self.assertEqual(a.query('SELECT 45 FROM pg_sleep(0.01)'), [['45']])
        self.assert_pool_free()

    def test_session_state_is_pinned_and_reset_only_at_frontend_close(self):
        a = self.client()
        a.query("SET application_name='retained-session'")
        original = self.pid(a)
        a.query('BEGIN')
        a.query('COMMIT')
        self.assertEqual(a.query('SHOW application_name'), [['retained-session']])
        self.assertEqual(self.pid(a), original)
        b = self.client()
        b.query('BEGIN')
        self.assertNotEqual(self.pid(b), original)
        b.query('ROLLBACK')
        a.close()
        b.close()
        self.idle()
        self.assert_pool_free()
        self.assertEqual(self.client().query('SHOW application_name'), [['']])

    def test_multistatement_transaction_releases_only_after_final_idle(self):
        a = self.client()
        a.query("BEGIN; INSERT INTO pool_data VALUES (7,'multi'); COMMIT")
        self.assertEqual(a.transaction_status, b'I')
        self.assertEqual(f.sql(self.fixture.nodes[0], 'SELECT value FROM pool_data WHERE id=7'), 'multi')
        self.assert_pool_free()

    def test_repeatable_read_snapshot_matches_direct_postgres(self):
        a = self.client()
        a.query('BEGIN ISOLATION LEVEL REPEATABLE READ')
        self.assertEqual(a.query('SELECT count(*) FROM pool_data'), [['0']])
        f.sql(self.fixture.nodes[0], "INSERT INTO pool_data VALUES (8,'outside')")
        self.assertEqual(a.query('SELECT count(*) FROM pool_data'), [['0']])
        self.assertEqual(f.sql(self.fixture.nodes[0], 'SELECT count(*) FROM pool_data'), '1')
        a.query('COMMIT')
        self.assertEqual(a.query('SELECT count(*) FROM pool_data'), [['1']])
        self.assert_pool_free()

    def test_read_committed_and_read_uncommitted_hide_dirty_writes(self):
        a, b = self.client(), self.client()
        a.query('BEGIN')
        a.query("INSERT INTO pool_data VALUES (9,'invisible')")
        for isolation in ('READ COMMITTED', 'READ UNCOMMITTED'):
            b.query(f'BEGIN ISOLATION LEVEL {isolation}')
            self.assertEqual(b.query('SELECT count(*) FROM pool_data'), [['0']])
            b.query('COMMIT')
        a.query('COMMIT')
        self.assertEqual(b.query('SELECT count(*) FROM pool_data'), [['1']])
        self.assert_pool_free()

    def test_serializable_conflict_is_native_and_not_replayed(self):
        f.sql(self.fixture.nodes[0], "INSERT INTO pool_data VALUES (10,'initial')")
        a, b = self.client(), self.client()
        for client in (a, b):
            client.query('BEGIN ISOLATION LEVEL SERIALIZABLE')
            self.assertEqual(client.query('SELECT value FROM pool_data WHERE id=10'), [['initial']])
        a.query("UPDATE pool_data SET value='winner' WHERE id=10")
        a.query('COMMIT')
        with self.assertRaises(f.PgError) as error:
            b.query("UPDATE pool_data SET value='loser' WHERE id=10")
        self.assertEqual(error.exception.code, '40001')
        b.query('ROLLBACK')
        self.assertEqual(f.sql(self.fixture.nodes[0], 'SELECT value FROM pool_data WHERE id=10'), 'winner')
        self.assert_pool_free()

    def test_cancelled_transaction_preserves_savepoint_before_release(self):
        a = self.client()
        a.query('BEGIN')
        original = self.pid(a)
        a.query("INSERT INTO pool_data VALUES (11,'before-cancel')")
        a.query('SAVEPOINT kept')
        query = 'SELECT 46 FROM pg_sleep(30)'
        a.send_query(query)
        self.active(query)
        self.cancel(a)
        with self.assertRaises(f.PgError) as error:
            a.read_cycle()
        self.assertEqual(error.exception.code, '57014')
        self.assertEqual(a.transaction_status, b'E')
        a.query('ROLLBACK TO SAVEPOINT kept')
        self.assertEqual(self.pid(a), original)
        a.query('COMMIT')
        self.assertEqual(f.sql(self.fixture.nodes[0], 'SELECT value FROM pool_data WHERE id=11'), 'before-cancel')
        self.assert_pool_free()

    def test_read_only_transaction_rejection_does_not_leak_state(self):
        a = self.client()
        a.query('BEGIN READ ONLY')
        with self.assertRaises(f.PgError) as error:
            a.query("INSERT INTO pool_data VALUES (12,'denied')")
        self.assertEqual(error.exception.code, '25006')
        a.query('ROLLBACK')
        a.query("INSERT INTO pool_data VALUES (12,'allowed')")
        self.assertEqual(f.sql(self.fixture.nodes[0], 'SELECT value FROM pool_data WHERE id=12'), 'allowed')
        self.assert_pool_free()


if __name__ == '__main__':
    evidence = Path(os.environ['TRANSACTION_POOL_EVIDENCE']).resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Transactions))
    print(output.getvalue(), end='')
    (evidence / 'unittest.log').write_text(output.getvalue())
    (evidence / 'result.json').write_text(json.dumps({'tests': result.testsRun, 'failures': len(result.failures),
        'errors': len(result.errors), 'skips': len(result.skipped), 'expected_failures': len(result.expectedFailures),
        'unexpected_successes': len(result.unexpectedSuccesses)}, indent=2) + '\n')
    raise SystemExit(not (result.wasSuccessful() and not result.skipped and not result.expectedFailures))
