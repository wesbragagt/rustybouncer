#!/usr/bin/env python3
"""PERF-008 W02–W12: complete shared-mode protocol positive scope, zero skips."""
import asyncio
import contextlib
import io
import json
import os
import socket
import struct
import subprocess
import unittest
from pathlib import Path

import failover as f
import protocol_support as p
import psycopg
from metrics_support import MetricsCluster
from pool_support import Client, PoolCase


class Protocol(PoolCase):
    evidence_env = 'PROTOCOL_POOL_EVIDENCE'

    def prepared(self, client, name=b'same', sql='SELECT $1::int + 1', types=(23,)):
        replies = p.cycle(client, p.parse(name, sql, types))
        self.assertEqual([tag for tag, _ in replies], [b'1', b'Z'])
        self.assertEqual(p.rows(replies), [])

    def call(self, client, name=b'same', values=(b'7',), **options):
        return p.rows(p.cycle(client, p.bind(name, values, **options) + p.execute(options.get('portal', b''))))

    def test_named_unnamed_binary_null_parameter_and_describe(self):
        a = self.client()
        for name in (b'', b'same'):
            self.prepared(a, name, 'SELECT $1::int, $2::text, $3::bytea', (23, 25, 17))
            for value in (1, 42, 987):
                data = p.bind(name, (struct.pack('!i', value), None, b'\0\xff'), formats=(1,), results=(1,))
                replies = p.cycle(a, data + p.frame(b'D', b'P\0') + p.execute())
                self.assertIn(b'T', [tag for tag, _ in replies])
                self.assertEqual(p.rows(replies), [[struct.pack('!i', value), None, b'\0\xff']])
            description = p.cycle(a, p.frame(b'D', b'S' + name + b'\0'))
            self.assertEqual([tag for tag, _ in description], [b't', b'T', b'Z'])
            self.assertEqual(description[0][1], struct.pack('!HIII', 3, 23, 25, 17))

    def test_flush_before_sync_preserves_implicit_transaction_commit_boundary(self):
        a = self.client()
        sql = "INSERT INTO pool_data VALUES(1,'only-at-sync') RETURNING id"
        a.sock.sendall(p.parse(b'write', sql) + p.frame(b'H'))
        self.assertEqual(p.receive(a, b'1')[-1][0], b'1')
        a.sock.sendall(p.bind(b'write') + p.frame(b'H'))
        self.assertEqual(p.receive(a, b'2')[-1][0], b'2')
        a.sock.sendall(p.execute() + p.frame(b'H'))
        self.assertEqual(p.rows(p.receive(a, b'C')), [[b'1']])
        self.assertEqual(f.sql(self.fixture.nodes[0], 'SELECT count(*) FROM pool_data'), '0')
        self.assertEqual(self.scrape().value('pool_backends', phase='leased'), 1)
        a.sock.sendall(p.frame(b'S'))
        self.assertEqual(p.receive(a), [(b'Z', b'I')])
        self.assertEqual(f.sql(self.fixture.nodes[0], 'SELECT value FROM pool_data'), 'only-at-sync')

    def test_pipeline_twelve_statements_response_order_and_error_sync(self):
        a = self.client()
        messages = b''.join(p.parse(f'q{i}'.encode(), f'SELECT {i}') + p.bind(f'q{i}'.encode())
                            + p.execute() + p.frame(b'S') for i in range(12))
        a.sock.sendall(messages)
        for index in range(12):
            replies = p.receive(a)
            self.assertEqual([tag for tag, _ in replies], [b'1', b'2', b'D', b'C', b'Z'])
            self.assertEqual(p.rows(replies), [[str(index).encode()]])
        messages = (p.parse(b'broken', 'SELECT 1/0') + p.bind(b'broken') + p.execute()
                    + p.parse(b'ignored', 'SELECT 666') + p.bind(b'ignored') + p.execute() + p.frame(b'H'))
        replies = p.cycle(a, messages)
        self.assertEqual([tag for tag, _ in replies], [b'1', b'E', b'Z'])
        self.assertEqual(f.PgError(replies[-2][1]).code, '22012')
        self.prepared(a, b'ignored', 'SELECT $1::int', (23,))
        self.assertEqual(self.call(a, b'ignored', (b'91',)), [[b'91']])

    def test_portal_suspension_transaction_lifetime_and_close(self):
        a = self.client()
        a.query('BEGIN')
        self.prepared(a, b'range', 'SELECT generate_series(1,3)', ())
        replies = p.cycle(a, p.bind(b'range', portal=b'cursor') + p.execute(b'cursor', 1))
        self.assertEqual(p.rows(replies), [[b'1']])
        self.assertEqual([tag for tag, _ in replies], [b'2', b'D', b's', b'Z'])
        self.assertEqual(a.transaction_status, b'T')
        self.assertEqual(p.rows(p.cycle(a, p.execute(b'cursor', 1))), [[b'2']])
        self.assertEqual(p.rows(p.cycle(a, p.execute(b'cursor'))), [[b'3']])
        self.assertEqual(p.cycle(a, p.frame(b'C', b'Pcursor\0')), [(b'3', b''), (b'Z', b'T')])
        a.query('COMMIT')
        replies = p.cycle(a, p.execute(b'cursor'))
        self.assertEqual(f.PgError(replies[0][1]).code, '34000')
        self.assertEqual(a.query('SELECT 17'), [['17']])

    def test_duplicate_names_close_and_independent_clients(self):
        a, b = self.client(), self.client()
        self.prepared(a, sql='SELECT $1::int+100')
        self.prepared(b, sql='SELECT $1::int+200')
        self.assertEqual(self.call(a), [[b'107']])
        self.assertEqual(self.call(b), [[b'207']])
        duplicate = p.cycle(a, p.parse(b'same', 'SELECT 999'))
        self.assertEqual(f.PgError(duplicate[0][1]).code, '42P05')
        self.assertEqual(self.call(a), [[b'107']])
        self.assertEqual(p.cycle(a, p.frame(b'C', b'Ssame\0')), [(b'3', b''), (b'Z', b'I')])
        missing = p.cycle(a, p.bind(b'same', (b'7',)) + p.execute())
        self.assertEqual(f.PgError(missing[0][1]).code, '26000')
        self.assertEqual(self.call(b), [[b'207']])

    def test_sql_and_protocol_namespace_visibility_deallocation_and_discard(self):
        a = self.client()
        self.prepared(a, sql='SELECT $1::int+3')
        self.assertEqual(a.query("SELECT name,from_sql,parameter_types::text FROM pg_prepared_statements ORDER BY name"),
                         [['same', 'f', '{integer}']])
        self.assertEqual(a.query('EXECUTE same(9)'), [['12']])
        with self.assertRaises(f.PgError) as error:
            a.query('PREPARE same AS SELECT 777')
        self.assertEqual(error.exception.code, '42P05')
        a.query('DEALLOCATE same')
        missing = p.cycle(a, p.bind(b'same', (b'9',)) + p.execute())
        self.assertEqual(f.PgError(missing[0][1]).code, '26000')
        a.query('PREPARE sql_only(int) AS SELECT $1+5')
        self.assertEqual(self.call(a, b'sql_only', (b'9',)), [[b'14']])
        self.assertEqual(a.query("SELECT from_sql FROM pg_prepared_statements WHERE name='sql_only'"), [['t']])
        a.query('DISCARD ALL')
        missing = p.cycle(a, p.bind(b'sql_only', (b'9',)) + p.execute())
        self.assertEqual(f.PgError(missing[0][1]).code, '26000')
        self.prepared(a)
        self.assertEqual(self.call(a), [[b'8']])

    def test_unnamed_replacement_error_and_simple_query_match_postgres(self):
        a = self.client()
        with contextlib.closing(Client(self.fixture.pg_port)) as direct:
            evidence = []
            for client in (direct, a):
                observed = []
                self.prepared(client, b'')
                observed.append(self.call(client, b''))
                failed = p.cycle(client, p.parse(b'', 'SELECT invalid syntax'))
                observed.append(f.PgError(next(body for tag, body in failed if tag == b'E')).code)
                missing = p.cycle(client, p.bind(b'', (b'7',)) + p.execute())
                observed.append(f.PgError(next(body for tag, body in missing if tag == b'E')).code)
                self.prepared(client, b'')
                client.query('SELECT 1')
                missing = p.cycle(client, p.bind(b'', (b'7',)) + p.execute())
                observed.append(f.PgError(next(body for tag, body in missing if tag == b'E')).code)
                evidence.append(observed)
            self.assertEqual(evidence[0], evidence[1])
        (self.output / 'unnamed-oracle.json').write_text(json.dumps(evidence, default=lambda x:x.decode(), indent=2)+'\n')

    def test_cancel_active_execute_savepoint_and_unnamed_sync_fence(self):
        a = self.client()
        self.prepared(a, b'', 'SELECT pg_sleep($1::float8),$2::int', (701, 23))
        a.sock.sendall(p.bind(b'', (b'30', b'19')) + p.execute() + p.frame(b'H'))
        f.eventually(lambda: f.sql(self.fixture.nodes[0], "SELECT count(*) FROM pg_stat_activity WHERE state='active' AND query LIKE 'SELECT pg_sleep($1%' AND pid<>pg_backend_pid()") == '1')
        self.cancel(a)
        a.sock.sendall(p.frame(b'S'))
        replies = p.receive(a)
        self.assertEqual(f.PgError(next(body for tag, body in replies if tag == b'E')).code, '57014')
        self.assertEqual(self.call(a, b'', (b'0', b'23')), [[b'', b'23']])
        a.query("BEGIN; INSERT INTO pool_data VALUES(2,'keep'); SAVEPOINT safe")
        self.prepared(a, b'sleep', 'SELECT pg_sleep(30)', ())
        a.sock.sendall(p.bind(b'sleep') + p.execute() + p.frame(b'H'))
        self.active('SELECT pg_sleep(30)')
        self.cancel(a)
        a.sock.sendall(p.frame(b'S'))
        replies = p.receive(a)
        self.assertEqual(f.PgError(next(body for tag, body in replies if tag == b'E')).code, '57014')
        self.assertEqual(a.transaction_status, b'E')
        a.query('ROLLBACK TO SAVEPOINT safe; COMMIT')
        self.assertEqual(f.sql(self.fixture.nodes[0], 'SELECT value FROM pool_data WHERE id=2'), 'keep')

    def test_disconnect_before_sync_rolls_back_executed_write(self):
        a = self.client()
        a.sock.sendall(p.parse(b'pending', "INSERT INTO pool_data VALUES(7,'rollback')") + p.bind(b'pending') + p.execute() + p.frame(b'H'))
        self.assertEqual(p.rows(p.receive(a, b'C')), [])
        self.assertEqual(f.sql(self.fixture.nodes[0], 'SELECT count(*) FROM pool_data'), '0')
        a.close()
        self.idle()
        b = self.client()
        self.assertEqual(b.query('SELECT count(*) FROM pool_data'), [['0']])
        self.prepared(b)
        self.assertEqual(self.call(b), [[b'8']])

    def test_simple_query_inside_extended_cycle_has_no_early_commit(self):
        a = self.client()
        a.sock.sendall(p.parse(b'pending', "INSERT INTO pool_data VALUES(8,'rollback-on-query-error')") + p.bind(b'pending') + p.execute() + p.frame(b'H'))
        self.assertEqual(p.rows(p.receive(a, b'C')), [])
        with self.assertRaises(f.PgError) as error:
            a.query('SELECT 1/0')
        self.assertEqual(error.exception.code, '22012')
        self.assertEqual(f.sql(self.fixture.nodes[0], 'SELECT count(*) FROM pool_data'), '0')
        self.assertEqual(a.query('SELECT 89'), [['89']])

    def test_libpq_pipeline_repeated_parameterized_statements(self):
        with psycopg.connect(host='127.0.0.1', port=self.fixture.proxy_port, user='postgres',
                            dbname='postgres', autocommit=True) as client:
            with client.pipeline():
                cursors = [client.execute('SELECT %s::int,%s::text', (index, f'libpq-{index}')) for index in range(12)]
            self.assertEqual([cursor.fetchall() for cursor in cursors], [[(index, f'libpq-{index}')] for index in range(12)])
            for index in range(12):
                self.assertEqual(client.execute('SELECT %s::int+1', (index,)).fetchall(), [(index+1,)])

    def test_extended_large_result_and_error_limit_do_not_expose_partial_rows(self):
        a = self.client()
        self.prepared(a, b'large', "SELECT repeat('r',4*1024*1024)", ())
        self.assertEqual(self.call(a, b'large', ()), [[b'r'*(4*1024*1024)]])
        self.prepared(a, b'overflow', "SELECT repeat('x',10*1024*1024) FROM generate_series(1,4)", ())
        a.sock.sendall(p.bind(b'overflow') + p.execute() + p.frame(b'S'))
        self.assertEqual(p.receive(a, b'2'), [(b'2', b'')])
        # The Execute response stays private until its native terminal. No D is
        # forwarded when aggregate exchange storage crosses the 32-MiB boundary.
        self.assertEqual(a.exact(1), b'E')
        size=struct.unpack('!I',a.exact(4))[0]
        error=f.PgError(a.exact(size-4))
        self.assertEqual(error.severity,'FATAL')
        self.assertEqual(error.code,'54000')
        a.close()
        self.idle()
        b=self.client()
        self.prepared(b)
        self.assertEqual(self.call(b),[[b'8']])

    def test_extended_metrics_are_separate_from_simple_queries_and_count_ignored_frames(self):
        a=self.client()
        f.eventually(lambda:self.scrape().value('extended_frames_in_flight')==0)
        before=self.scrape()
        self.prepared(a,b'division','SELECT 1/$1::int',(23,))
        self.assertEqual(self.call(a,b'division',(b'2',)),[[b'0']])
        replies=p.cycle(a,p.bind(b'division',(b'0',))+p.execute())
        self.assertEqual(f.PgError(next(body for tag,body in replies if tag==b'E')).code,'22012')
        def complete():
            snapshot=self.scrape()
            return snapshot if snapshot.value('extended_frames_in_flight')==snapshot.value('extended_cycles_in_flight')==0 else None
        after=f.eventually(complete)
        self.assertEqual(after.total('extended_frames_total')-before.total('extended_frames_total'),8)
        self.assertEqual(after.value('extended_cycles_total',outcome='ok')-before.value('extended_cycles_total',outcome='ok'),2)
        self.assertEqual(after.value('extended_cycles_total',outcome='sql_error')-before.value('extended_cycles_total',outcome='sql_error'),1)
        self.assertEqual(after.total('queries_total'),before.total('queries_total'))
        self.assertEqual(after.total('query_attempts_total'),before.total('query_attempts_total'))
        self.assertEqual(after.value('extended_backend_exchanges_total',outcome='sql_error')-before.value('extended_backend_exchanges_total',outcome='sql_error'),1)
        expected=7 if self.pool_mode=='session' else 12
        self.assertEqual(after.total('extended_backend_exchanges_total')-before.total('extended_backend_exchanges_total'),expected)
        (self.output/'protocol-metrics.prom').write_text(after.text)

    def test_extended_session_effects_and_deallocation_keep_native_namespace(self):
        a=self.client()
        self.prepared(a,b'setting',"SET application_name='extended-session'",())
        self.assertEqual(self.call(a,b'setting',()),[])
        self.assertEqual(a.query('SHOW application_name'),[['extended-session']])
        self.prepared(a,b'victim','SELECT 3',())
        self.prepared(a,b'remove','DEALLOCATE victim',())
        self.assertEqual(self.call(a,b'remove',()),[])
        missing=p.cycle(a,p.bind(b'victim')+p.execute())
        self.assertEqual(f.PgError(next(body for tag,body in missing if tag==b'E')).code,'26000')
        a.close()
        self.idle()
        b=self.client()
        self.assertEqual(b.query('SHOW application_name'),[['']])
        self.assertEqual(b.query("SELECT count(*) FROM pg_prepared_statements"),[['0']])

    def test_cancellation_during_partial_execute_delivery_closes_and_refunds(self):
        a=self.client()
        a.sock.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,4096)
        self.prepared(a,b'large',"SELECT repeat('z',10*1024*1024)",())
        a.sock.sendall(p.bind(b'large')+p.frame(b'H'))
        self.assertEqual(p.receive(a,b'2'),[(b'2',b'')])
        a.sock.sendall(p.execute()+p.frame(b'H'))
        self.assertEqual(a.exact(1),b'D')
        size=struct.unpack('!I',a.exact(4))[0]
        self.assertGreater(size,10*1024*1024)
        self.cancel(a)
        self.idle()
        after=self.scrape()
        self.assertEqual(after.value('extended_frames_total',kind='execute',outcome='delivery_error'),1)
        self.assertEqual(after.value('extended_cycles_total',outcome='delivery_error'),1)
        self.assertEqual(after.value('extended_cycles_in_flight'),0)
        # Only a prefix of the row may already be in kernel buffers. The owner
        # closes; it never inserts an ErrorResponse into a partially sent row.
        a.close()
        b=self.client()
        self.prepared(b)
        self.assertEqual(self.call(b),[[b'8']])
        (self.output/'delivery-cancel.json').write_text(json.dumps({'row_bytes':size-4,
            'partial_delivery_observed':True,'closed_without_replay':True,'leased_result_bytes_after':0})+'\n')

    def test_bound_portal_outlives_close_statement_until_transaction_end(self):
        a=self.client()
        a.query('BEGIN')
        self.prepared(a,b'temporary-statement','SELECT $1::int',(23,))
        replies=p.cycle(a,p.bind(b'temporary-statement',(b'39',),portal=b'retained')+p.frame(b'C',b'Stemporary-statement\0'))
        self.assertEqual([tag for tag,_ in replies],[b'2',b'3',b'Z'])
        self.assertEqual(p.rows(p.cycle(a,p.execute(b'retained'))),[[b'39']])
        a.query('COMMIT')

    def test_slow_execute_receiver_delivery_deadline_refunds_ownership(self):
        self.launch(delivery_timeout_ms=500)
        a=self.client()
        a.sock.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,4096)
        self.prepared(a,b'large',"SELECT repeat('z',10*1024*1024)",())
        a.sock.sendall(p.bind(b'large')+p.frame(b'H'))
        p.receive(a,b'2')
        a.sock.sendall(p.execute()+p.frame(b'H'))
        self.assertEqual(a.exact(1),b'D')
        self.idle()
        self.assertEqual(self.scrape().value('extended_frames_total',kind='execute',outcome='delivery_error'),1)
        a.close()
        b=self.client()
        self.prepared(b)
        self.assertEqual(self.call(b),[[b'8']])

    def test_malformed_extended_frame_is_protocol_error_before_dispatch(self):
        a=self.client()
        before=self.scrape().total('extended_backend_exchanges_total')
        with self.assertRaises(f.PgError) as error:
            p.cycle(a,p.frame(b'P',b''))
        self.assertEqual(error.exception.code,'08P01')
        self.assertEqual(error.exception.severity,'FATAL')
        a.close()
        self.idle()
        self.assertEqual(self.scrape().total('extended_backend_exchanges_total'),before)

    def test_copy_rejection_is_explicit_and_does_not_contaminate_next_owner(self):
        a=self.client()
        self.prepared(a,b'copy','COPY (SELECT 1) TO STDOUT',())
        with self.assertRaises(f.PgError) as error:
            p.cycle(a,p.bind(b'copy')+p.execute())
        self.assertEqual(error.exception.code,'0A000')
        self.assertEqual(error.exception.severity,'FATAL')
        a.close()
        self.idle()
        b=self.client()
        self.prepared(b)
        self.assertEqual(self.call(b),[[b'8']])

    def test_unknown_function_planning_effect_keeps_session_affinity_before_execute(self):
        f.sql(self.fixture.nodes[0], "CREATE FUNCTION plan_effect(i int) RETURNS int LANGUAGE plpgsql IMMUTABLE AS $$ "
              "BEGIN PERFORM set_config('application_name','bind-planning-effect',false); RETURN i; END $$")
        observations=[]
        with contextlib.closing(Client(self.fixture.pg_port)) as direct:
            for client in (direct,self.client()):
                self.prepared(client,b'', 'SELECT plan_effect(7)',())
                replies=p.cycle(client,p.bind(b''))
                self.assertEqual([tag for tag,_ in replies],[b'2',b'S',b'Z'])
                self.assertIn((b'S',b'application_name\0bind-planning-effect\0'),replies)
                # No Execute was sent. PostgreSQL may fold immutable functions
                # during planning at Bind; preserve the observed native state.
                observations.append(client.query('SHOW application_name'))
        self.assertEqual(observations,[[['bind-planning-effect']],[['bind-planning-effect']]])
        (self.output/'planning-oracle.json').write_text(json.dumps(observations)+'\n')

    def test_result_type_change_never_delivers_binary_using_old_description(self):
        f.sql(self.fixture.nodes[0],'CREATE TABLE result_shape(value int); INSERT INTO result_shape VALUES(42)')
        a=self.client()
        with contextlib.closing(Client(self.fixture.pg_port)) as direct:
            for client in (direct,a):
                self.prepared(client,b'shape','SELECT value FROM result_shape',())
            f.sql(self.fixture.nodes[0],'ALTER TABLE result_shape ALTER COLUMN value TYPE bigint')
            with self.assertRaises(f.PgError) as expected:
                p.rows(p.cycle(direct,p.bind(b'shape',results=(1,))+p.execute()))
            self.assertEqual(expected.exception.code,'0A000')
            with self.assertRaises(f.PgError) as observed:
                p.rows(p.cycle(a,p.bind(b'shape',results=(1,))+p.execute()))
            self.assertEqual(observed.exception.code,'0A000')
            if self.pool_mode=='transaction':
                self.assertEqual(observed.exception.severity,'FATAL')
        a.close()
        self.idle()
        b=self.client()
        self.prepared(b,b'new-shape','SELECT value FROM result_shape',())
        self.assertEqual(self.call(b,b'new-shape',(),results=(1,)),[[struct.pack('!q',42)]])

    def test_real_primary_promotion_fences_partial_write_and_recovers_new_work(self):
        cluster=MetricsCluster(self.output/'streaming-cluster')
        self.addCleanup(cluster.close)
        cluster.start()
        cluster.stop_proxy()
        path=Path(cluster.directory.name)/'config.toml'
        config=f'pool_mode = "{self.pool_mode}"\npool_size = 2\nmax_pool_backends = 4\n'+path.read_text()
        path.write_text(config)
        (self.output/'promotion-config.toml').write_text(config)
        cluster.process=subprocess.Popen([str(f.BINARY),str(path)],stdout=cluster.log,stderr=cluster.log)
        f.eventually(cluster.ready)
        idle=cluster.connect()
        self.addCleanup(idle.close)
        self.prepared(idle,b'survive','SELECT $1::int+7',(23,))
        writer=cluster.connect()
        self.addCleanup(writer.close)
        writer.sock.sendall(p.parse(b'write',"INSERT INTO continuity VALUES(900,'must-not-replay') RETURNING id")
                            +p.bind(b'write')+p.execute()+p.frame(b'H'))
        self.assertEqual(p.rows(p.receive(writer,b'C')),[[b'900']])
        self.assertEqual(f.sql(cluster.nodes[0],'SELECT count(*) FROM continuity'),'0')
        cluster.pause(0)
        cluster.promote(1)
        f.eventually(lambda:cluster.scrape().value('node_role',node='b',role='primary')==1)
        with self.assertRaises((f.PgError,OSError)) as error:
            p.cycle(writer,b'')
        if isinstance(error.exception,f.PgError):
            self.assertEqual(error.exception.code,'08006')
        self.assertEqual(f.sql(cluster.nodes[1],'SELECT count(*) FROM continuity WHERE id=900'),'0')
        if self.pool_mode=='transaction':
            self.assertEqual(self.call(idle,b'survive',(b'5',)),[[b'12']])
        else:
            with self.assertRaises((f.PgError,OSError)):
                self.call(idle,b'survive',(b'5',))
        with contextlib.closing(cluster.connect()) as recovered:
            self.prepared(recovered,b'primary',"SELECT current_setting('cluster_name')",())
            self.assertEqual(self.call(recovered,b'primary',()),[[b'b']])
            self.prepared(recovered,b'newwrite',"INSERT INTO continuity VALUES(901,'new-primary') RETURNING id",())
            self.assertEqual(self.call(recovered,b'newwrite',()),[[b'901']])
        self.assertEqual(f.sql(cluster.nodes[1],'SELECT payload FROM continuity WHERE id=901'),'new-primary')
        (self.output/'promotion.json').write_text(json.dumps({'promoted':'b','unfinished_write_replayed':False,
            'idle_prepared_client_continued':self.pool_mode=='transaction','new_primary_prepared_write_committed':True})+'\n')

    def concurrent_prepared(self, count):
        self.launch(wait_timeout_ms=60000, pool_size=2, max_pool_backends=2)
        async def run():
            clients = []
            async def connect():
                client = await p.AsyncClient.connect(self.fixture.proxy_port)
                clients.append(client)
            try:
                await asyncio.wait_for(asyncio.gather(*(connect() for _ in range(count))), 90)
                self.assertEqual(len(clients), count)
                self.assertEqual(self.scrape().value('clients_active'), count)
                physical = int(f.sql(self.fixture.nodes[0], "SELECT count(*) FROM pg_stat_activity WHERE backend_type='client backend' AND usename='postgres' AND pid<>pg_backend_pid()"))
                self.assertLessEqual(physical, 2)
                async def worker(client, index):
                    try:
                        name = b'same-name-all-clients'
                        sql = f'SELECT $1::int+{index},$2::text'
                        self.assertEqual(p.rows(await client.cycle(p.parse(name, sql, (23, 25)))), [])
                        result = []
                        for cycle in range(3):
                            nonce = f'private-client-{index}-cycle-{cycle}'.encode()
                            replies = await client.cycle(p.bind(name, (str(cycle).encode(), nonce)) + p.execute())
                            self.assertEqual(p.rows(replies), [[str(index+cycle).encode(), nonce]])
                            result.append(index+cycle)
                        return result
                    finally:
                        await client.close()
                results = await asyncio.wait_for(asyncio.gather(*(worker(client, index) for index, client in enumerate(clients))), 90)
                return {'ready_frontends': count, 'physical_backends_at_barrier': physical, 'cycles': 3,
                        'correct_results': sum(map(len, results)), 'errors': 0, 'reconnects': 0}
            finally:
                await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)
        result = asyncio.run(run())
        self.idle()
        self.assertEqual(self.scrape().value('pool_waiters'), 0)
        (self.output / 'concurrency.json').write_text(json.dumps(result, indent=2)+'\n')

    def test_10_prepared_clients(self):
        self.concurrent_prepared(10)

    def test_100_prepared_clients(self):
        self.concurrent_prepared(100)

    def test_1000_prepared_clients(self):
        self.concurrent_prepared(1000)


class SessionProtocol(Protocol):
    evidence_env = 'PROTOCOL_SESSION_EVIDENCE'
    pool_mode = 'session'


class TransactionProtocol(Protocol):
    evidence_env = 'PROTOCOL_TRANSACTION_EVIDENCE'
    pool_mode = 'transaction'

    def test_forced_reassignment_retains_resolved_types_and_all_seventeen_definitions(self):
        a = self.client()
        a.query('BEGIN')
        original = self.pid(a)
        for index in range(17):
            self.prepared(a, f'statement-{index}'.encode(), f'SELECT $1::int+{index}', ())
        a.query('COMMIT')
        holders = [self.client(), self.client()]
        assigned = []
        for holder in holders:
            holder.query('BEGIN')
            assigned.append(self.pid(holder))
        alternative = next(index for index, pid in enumerate(assigned) if pid != original)
        holders[alternative].query('ROLLBACK')
        a.query('BEGIN')
        self.assertNotEqual(self.pid(a), original)
        for index in range(17):
            self.assertEqual(self.call(a, f'statement-{index}'.encode(), (b'9',)), [[str(index+9).encode()]])
        a.query('COMMIT')
        for holder in holders:
            holder.query('ROLLBACK')
        (self.output / 'reassignment.json').write_text(json.dumps({'original_pid':original,'assigned_pids':assigned,'definitions':17})+'\n')

    def test_original_inferred_parameter_types_survive_schema_change(self):
        f.sql(self.fixture.nodes[0], 'CREATE TABLE inference(value int); INSERT INTO inference VALUES(42)')
        a=self.client()
        self.prepared(a,b'inferred','SELECT 42::int FROM inference WHERE value=$1',())
        f.sql(self.fixture.nodes[0], 'ALTER TABLE inference ALTER COLUMN value TYPE bigint')
        description=p.cycle(a,p.frame(b'D',b'Sinferred\0'))
        self.assertEqual(description[0],(b't',struct.pack('!HI',1,23)))
        self.assertEqual(self.call(a,b'inferred',(b'42',)),[[b'42']])
        (self.output/'type-oracle.json').write_text(json.dumps({'original_oid':23,'new_table_oid':20,'restored_oid':23})+'\n')

    def test_count_and_byte_limits_fail_closed_without_committing_prior_write(self):
        for byte_quota in (False, True):
            a = self.client()
            if byte_quota:
                self.prepared(a, b'large', 'SELECT 1 /*' + 'x'*600000 + '*/', ())
                self.prepared(a, b'write', "INSERT INTO pool_data VALUES(90,'must-rollback')", ())
                a.sock.sendall(p.bind(b'write') + p.execute() + p.frame(b'H'))
                p.receive(a, b'C')
                data = p.parse(b'overflow', 'SELECT 2 /*' + 'y'*600000 + '*/')
            else:
                for index in range(128):
                    self.prepared(a, str(index).encode(), 'SELECT 1', ())
                data = p.parse(b'overflow', 'SELECT 2')
            with self.assertRaises(f.PgError) as error:
                p.cycle(a, data)
            self.assertEqual(error.exception.code, '54000')
            a.close()
            self.idle()
            self.assertEqual(f.sql(self.fixture.nodes[0], 'SELECT count(*) FROM pool_data'), '0')
            b = self.client()
            self.prepared(b)
            self.assertEqual(self.call(b), [[b'8']])
            b.close()
            self.idle()

    def test_portal_count_is_bounded_even_after_its_statement_name_closes(self):
        a=self.client()
        a.query('BEGIN')
        self.prepared(a,b'source','SELECT $1::int',(23,))
        for index in range(128):
            replies=p.cycle(a,p.bind(b'source',(b'7',),portal=f'portal-{index}'.encode()))
            self.assertEqual([tag for tag,_ in replies],[b'2',b'Z'])
        replies=p.cycle(a,p.frame(b'C',b'Ssource\0'))
        self.assertEqual([tag for tag,_ in replies],[b'3',b'Z'])
        self.assertEqual(p.rows(p.cycle(a,p.execute(b'portal-0'))),[[b'7']])
        self.prepared(a,b'new-source','SELECT 8',())
        with self.assertRaises(f.PgError) as error:
            p.cycle(a,p.bind(b'new-source',portal=b'overflow'))
        self.assertEqual(error.exception.code,'54000')
        a.close()
        self.idle()
        b=self.client()
        self.prepared(b)
        self.assertEqual(self.call(b),[[b'8']])

    def test_waiting_parse_cancellation_and_eof_refund_without_dispatch(self):
        a, b, waiting = self.client(), self.client(), self.client()
        for holder in (a, b):
            holder.query('BEGIN')
        waiting.sock.sendall(p.parse(b'waiting', "INSERT INTO pool_data VALUES(91,'never-dispatched')") + p.frame(b'H'))
        f.eventually(lambda:self.scrape().value('pool_waiters') == 1)
        self.cancel(waiting)
        replies = p.receive(waiting, b'E')
        self.assertEqual(f.PgError(replies[0][1]).code, '57014')
        waiting.sock.sendall(p.frame(b'S'))
        self.assertEqual(p.receive(waiting), [(b'Z', b'I')])
        self.assertEqual(self.scrape().value('pool_waiters'), 0)
        waiting.sock.sendall(p.parse(b'abandoned', "INSERT INTO pool_data VALUES(92,'never-dispatched')") + p.frame(b'H'))
        f.eventually(lambda:self.scrape().value('pool_waiters') == 1)
        waiting.close()
        f.eventually(lambda:self.scrape().value('pool_waiters') == 0)
        for holder in (a, b):
            holder.query('ROLLBACK')
        self.assertEqual(f.sql(self.fixture.nodes[0], 'SELECT count(*) FROM pool_data'), '0')


if __name__ == '__main__':
    evidence = Path(os.environ['PROTOCOL_POOL_EVIDENCE']).resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    os.environ['PROTOCOL_SESSION_EVIDENCE'] = str(evidence / 'session')
    os.environ['PROTOCOL_TRANSACTION_EVIDENCE'] = str(evidence / 'transaction')
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(case)
                               for case in (SessionProtocol, TransactionProtocol))
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    print(output.getvalue(), end='')
    (evidence / 'unittest.log').write_text(output.getvalue())
    (evidence / 'result.json').write_text(json.dumps({'tests':result.testsRun,'failures':len(result.failures),
        'errors':len(result.errors),'skips':len(result.skipped),'expected_failures':len(result.expectedFailures),
        'unexpected_successes':len(result.unexpectedSuccesses)}, indent=2)+'\n')
    raise SystemExit(not(result.wasSuccessful() and not result.skipped and not result.expectedFailures))
