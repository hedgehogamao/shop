"""Collector concurrency regressions, using controlled fetches without networking."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from pricemonitor.app import Checker
from pricemonitor.extractor import Quote
from pricemonitor.store import Store


class CheckerConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'prices.sqlite3')
        self.product_id = self.store.create({
            'url': 'https://shop.example/gpu', 'name': '显卡', 'selector': '',
            'currency': 'CNY', 'target_price': '100', 'interval_minutes': 15,
        })
        self.checkers = []

    async def asyncTearDown(self):
        for checker in self.checkers:
            await checker.close()
        self.temp.cleanup()

    def checker(self, fetcher):
        checker = Checker(self.store, fetcher)
        self.checkers.append(checker)
        return checker

    async def test_overlapping_checks_wait_for_one_shared_fetch(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def fetch(*args):
            calls.append(args)
            entered.set()
            await release.wait()
            return Quote('显卡', '90', 'CNY')

        checker = self.checker(fetch)
        first = asyncio.create_task(checker.check(self.product_id))
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(checker.check(self.product_id))
        await asyncio.sleep(0)
        self.assertFalse(second.done(), 'A refresh must await an already running fetch.')
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store.get(self.product_id)['latest_price'], '90')
        self.assertEqual(len(self.store.get(self.product_id)['history']), 1)

    async def test_source_edit_during_fetch_refreshes_the_updated_source(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def fetch(url, selector, currency):
            calls.append(selector)
            if not selector:
                entered.set()
                await release.wait()
                return Quote('旧来源', '99', currency)
            return Quote('新来源', '88', currency)

        checker = self.checker(fetch)
        first = asyncio.create_task(checker.check(self.product_id))
        await asyncio.wait_for(entered.wait(), 2)
        self.store.update(self.product_id, {'selector': '.current-price'})
        second = asyncio.create_task(checker.check(self.product_id))
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 2)
        current = self.store.get(self.product_id)
        self.assertEqual(calls, ['', '.current-price'])
        self.assertEqual(current['latest_price'], '88')
        self.assertEqual([point['price'] for point in current['history']], ['88'])

    async def test_queued_scheduled_check_respects_pause_but_manual_refresh_runs(self):
        calls = []

        async def fetch(*args):
            calls.append(args)
            return Quote('显卡', '90', 'CNY')

        checker = self.checker(fetch)
        checker.semaphore = asyncio.Semaphore(1)
        await checker.semaphore.acquire()
        pending = asyncio.create_task(checker.check(self.product_id, scheduled=True))
        # Let the wrapper and its fetch task reach the occupied semaphore.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.store.update(self.product_id, {'enabled': False})
        checker.semaphore.release()
        self.assertFalse(await asyncio.wait_for(pending, 2))
        self.assertEqual(calls, [])
        self.assertIsNone(self.store.get(self.product_id)['latest_price'])
        self.assertTrue(await asyncio.wait_for(checker.check(self.product_id), 2))
        self.assertEqual(len(calls), 1)

    async def test_shutdown_cancels_running_fetch_without_writing_a_result(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def fetch(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        checker = self.checker(fetch)
        pending = asyncio.create_task(checker.check(self.product_id))
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(checker.close(), 2)
        results = await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 2)
        self.assertTrue(cancelled.is_set())
        self.assertIsInstance(results[0], asyncio.CancelledError)
        self.assertEqual(self.store.get(self.product_id)['history'], [])
        self.assertIsNone(self.store.get(self.product_id)['last_checked'])

    async def test_cancelling_one_waiter_preserves_the_shared_fetch(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def fetch(*args):
            calls.append(args)
            entered.set()
            await release.wait()
            return Quote('显卡', '90', 'CNY')

        checker = self.checker(fetch)
        first = asyncio.create_task(checker.check(self.product_id))
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(checker.check(self.product_id))
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        release.set()
        self.assertTrue(await asyncio.wait_for(second, 2))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store.get(self.product_id)['latest_price'], '90')


if __name__ == '__main__':
    unittest.main()
