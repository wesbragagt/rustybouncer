"""Adversarial lifecycle tests: no external endpoints or capability skips."""
import asyncio
import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from conformance.base import Case
from conformance.client import Client, bounded, stop_owned
from conformance.config import Contract, validate_settings


def fixture(**settings):
    case = Case()
    case.contract = Contract('deadlines', 'routing', {}, settings)
    case.evidence = []
    case.clients = []
    case.schema = 'deadline_test'
    case.postgres_dsn = 'unused'
    return case


class Deadlines(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_joins_before_disposal(self):
        events = []
        async def operation():
            try:
                await asyncio.Event().wait()
            finally:
                events.append('joined')
        with self.assertRaises(TimeoutError):
            await bounded(operation(), .01, lambda: events.append('disposed'))
        self.assertEqual(events, ['joined', 'disposed'])

    async def test_caller_cancellation_joins(self):
        entered = asyncio.Event()
        stopped = asyncio.Event()
        async def operation():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        owner = asyncio.create_task(bounded(operation(), 10, lambda: None))
        await entered.wait()
        owner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await owner
        self.assertTrue(stopped.is_set())

    async def test_error_preserves_disposal_error(self):
        async def operation():
            await asyncio.Event().wait()
        def abort():
            raise OSError('disposal')
        with self.assertRaises(ExceptionGroup) as caught:
            await bounded(operation(), .01, abort)
        self.assertEqual([type(e) for e in caught.exception.exceptions], [TimeoutError, OSError])

    async def test_uncooperative_task_is_reported(self):
        release = asyncio.Event()
        async def operation():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
        task = asyncio.create_task(operation())
        await asyncio.sleep(0)
        try:
            with self.assertRaisesRegex(RuntimeError, 'cannot forcibly stop'):
                await stop_owned(task, .01)
        finally:
            release.set()
            await task

    async def test_eventually_bounds_predicate(self):
        case = fixture(recovery_timeout=.02)
        with self.assertRaises(AssertionError):
            await case.eventually(lambda: asyncio.Event().wait(), 'never')

    async def test_pending_registers_cleanup_before_observation(self):
        case = fixture(observation_window=1)
        task = asyncio.create_task(asyncio.sleep(10))
        observer = asyncio.create_task(case.pending(task))
        await asyncio.sleep(0)
        observer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await observer
        self.assertEqual(len(case._cleanups), 1)
        await case.stop_task(task)
        self.assertTrue(task.cancelled())

    async def test_all_cleanup_attempted(self):
        case = fixture()
        failed = AsyncMock()
        failed.close.side_effect = ValueError('close')
        other = AsyncMock()
        cleanup = AsyncMock()
        case.clients = [other, failed]
        with patch('conformance.base.Client.connect', AsyncMock(return_value=cleanup)):
            with self.assertRaises(ExceptionGroup):
                await case.drop_schema()
        other.close.assert_awaited_once()
        cleanup.query.assert_awaited_once()
        cleanup.close.assert_awaited_once()

    async def test_partial_setup_registers_cleanup(self):
        case = fixture()
        with patch.object(case, 'connect', AsyncMock(side_effect=OSError('setup'))):
            with self.assertRaises(OSError):
                await case.asyncSetUp()
        self.assertEqual(case._cleanups[0][0], case.drop_schema)

    async def test_adapter_inherited_pipe_deadline(self):
        case = fixture(recovery_timeout=.1, cleanup_timeout=1)
        case.contract.actions['pipe'] = [sys.executable, '-c',
            'import os,time; pid=os.fork(); time.sleep(30) if pid == 0 else os._exit(0)']
        started = time.monotonic()
        with patch.dict(os.environ, CONFORMANCE_ALLOW_FAULTS='1'):
            with self.assertRaises(TimeoutError):
                await case.action('pipe')
        self.assertLess(time.monotonic() - started, 2)

    async def test_invalid_configuration(self):
        for value in (0, -1, float('nan'), float('inf'), True, '1'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_settings({}, {'cleanup_timeout': value})
                with self.assertRaises(ValueError):
                    fixture(cleanup_timeout=value).contract.number('cleanup_timeout', 1)

    async def test_connect_late_success_is_disposed(self):
        connection = Mock()
        async def acquire(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return connection
        with patch('conformance.client.AsyncConnection.connect', acquire):
            with self.assertRaises(TimeoutError):
                await Client.connect('unused', .01, [])
        connection.pgconn.finish.assert_called_once()

    async def test_close_is_bounded(self):
        connection = Mock()
        connection.close = AsyncMock(side_effect=lambda: None)
        async def hang():
            await asyncio.Event().wait()
        connection.close.side_effect = hang
        client = Client(connection, .01, [])
        with self.assertRaises(TimeoutError):
            await client.close()
        connection.pgconn.finish.assert_called_once()

    async def test_body_and_cleanup_failures_both_retained(self):
        class Broken(unittest.IsolatedAsyncioTestCase):
            async def asyncSetUp(self):
                async def fail_cleanup():
                    raise OSError('cleanup diagnostic')
                self.addAsyncCleanup(fail_cleanup)
            async def runTest(self):
                raise ValueError('body diagnostic')
        # Run a separate event loop in a thread; outer test asserts both errors.
        result = unittest.TestResult()
        await asyncio.to_thread(Broken().run, result)
        self.assertEqual(len(result.errors), 2)
        self.assertIn('body diagnostic', result.errors[0][1])
        self.assertIn('cleanup diagnostic', result.errors[1][1])

    async def test_adapter_caller_cancellation(self):
        case = fixture(recovery_timeout=10, cleanup_timeout=1)
        case.contract.actions['hang'] = [sys.executable, '-c', 'import time; time.sleep(30)']
        with patch.dict(os.environ, CONFORMANCE_ALLOW_FAULTS='1'):
            task = asyncio.create_task(case.action('hang'))
            await asyncio.sleep(.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
