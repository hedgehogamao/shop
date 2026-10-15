import asyncio
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from pricemonitor.app import Checker, create_app
from pricemonitor.extractor import PriceError, Quote
from pricemonitor.store import Store


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'prices.sqlite3'
        self.response = Quote('测试显卡', '2499.00', 'CNY')
        self.calls = []

        async def fetch(url, selector, currency):
            self.calls.append((url, selector, currency))
            if isinstance(self.response, Exception):
                raise self.response
            return self.response

        self.fetch = fetch
        self.client = TestClient(create_app(self.path, self.fetch, scheduler=False), base_url='http://localhost')
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def add(self, **kwargs):
        payload = {'url': 'https://shop.example/products/gpu', 'target_price': '2500', **kwargs}
        response = self.client.post('/api/products', json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_price_history_target_and_edit(self):
        product = self.add()
        self.assertEqual(product['name'], '测试显卡')
        self.assertEqual(product['latest_price'], '2499.00')
        self.assertTrue(product['alert'])
        self.response = Quote('测试显卡', '2399.00', 'CNY')
        result = self.client.post(f"/api/products/{product['id']}/check").json()
        self.assertEqual([p['price'] for p in result['history']], ['2499.00', '2399.00'])
        self.assertEqual(result['previous_price'], '2499.00')
        result = self.client.patch(f"/api/products/{product['id']}", json={'target_price': '2000', 'name': '我的显卡'}).json()
        self.assertFalse(result['alert'])
        self.assertEqual(result['name'], '我的显卡')
        self.assertEqual(self.client.get('/api/health').json()['status'], 'ok')

    def test_duplicate_and_invalid_inputs(self):
        self.add()
        self.assertEqual(self.client.post('/api/products', json={'url': 'https://shop.example/products/gpu#reviews'}).status_code, 409)
        for change in ({'target_price': 'NaN'}, {'target_price': '-1'}, {'interval_minutes': 0},
                       {'url': 'https://user:password@shop.example/gpu'}, {'url': 'https://shop.example:8000/gpu'},
                       {'currency': 'FAKE'}):
            with self.subTest(change=change):
                self.assertEqual(self.client.post('/api/products', json={'url': 'https://shop.example/other', **change}).status_code, 422)

    def test_failures_retain_history_without_false_alerts(self):
        product = self.add()
        self.response = PriceError('无法找到价格')
        failed = self.client.post(f"/api/products/{product['id']}/check").json()
        self.assertEqual(failed['latest_price'], '2499.00')
        self.assertEqual(failed['error'], '无法找到价格')
        self.assertFalse(failed['alert'])
        self.assertEqual(len(failed['history']), 1)
        self.response = Quote('测试显卡', '100.00', 'USD')
        mismatch = self.client.post(f"/api/products/{product['id']}/check").json()
        self.assertIn('货币', mismatch['error'])
        self.assertEqual(len(mismatch['history']), 1)
        self.response = Quote('测试显卡', '2399.00', 'CNY')
        recovered = self.client.post(f"/api/products/{product['id']}/check").json()
        self.assertIsNone(recovered['error'])
        self.assertTrue(recovered['alert'])

    def test_pause_schedule_and_manual_check(self):
        product = self.add()
        self.client.patch(f"/api/products/{product['id']}", json={'enabled': False})
        self.calls.clear()
        checker = Checker(self.client.app.state.store, self.fetch)
        asyncio.run(checker.due_once())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.client.post('/api/check-all').json()['checked'], 0)
        self.client.post(f"/api/products/{product['id']}/check")
        self.assertEqual(len(self.calls), 1)
        self.client.patch(f"/api/products/{product['id']}", json={'enabled': True})
        self.calls.clear()
        asyncio.run(checker.due_once())
        self.assertEqual(self.calls, [])  # Not due immediately after a successful check.

    def test_source_changes_reject_inflight_result_and_restart_persistence(self):
        product = self.add()
        store = self.client.app.state.store
        changed = self.client.patch(f"/api/products/{product['id']}", json={'selector': '.offer-price'}).json()
        self.assertIsNone(changed['latest_price'])
        self.assertEqual(changed['history'], [])
        store.result(product['id'], product['source_version'], quote=self.response)
        self.assertEqual(store.get(product['id'])['history'], [])
        self.client.post(f"/api/products/{product['id']}/check")
        with TestClient(create_app(self.path, self.fetch, scheduler=False), base_url='http://localhost') as restarted:
            restored = restarted.get('/api/products').json()
            self.assertEqual(restored[0]['latest_price'], '2499.00')
            self.assertEqual(len(restored[0]['history']), 1)
        self.assertEqual(self.client.delete(f"/api/products/{product['id']}").status_code, 204)
        store.result(product['id'], changed['source_version'], quote=self.response)
        self.assertEqual(self.client.get('/api/products').json(), [])

    def test_currency_change_clears_old_target(self):
        product = self.add()
        result = self.client.patch(f"/api/products/{product['id']}", json={'currency': 'USD'}).json()
        self.assertIsNone(result['target_price'])
        self.assertIsNone(result['latest_price'])
        self.response = Quote('测试显卡', '100.00', 'USD')
        result = self.client.post(f"/api/products/{product['id']}/check").json()
        self.assertFalse(result['alert'])

    def test_manual_reference_is_not_a_live_quote_or_target(self):
        reference = self.client.get('/api/build-reference').json()
        self.assertEqual(reference['reference_price'], '8239.00')
        self.assertFalse(reference['verified_live'])
        self.assertIsNone(reference['target_price'])
        self.assertIsNone(reference['url'])
        self.assertEqual(self.client.get('/api/products').json(), [])

    def test_browser_security_and_static_page(self):
        blocked = self.client.post('/api/check-all', headers={'origin': 'https://unrelated.example'})
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(self.client.get('/api/health', headers={'host': 'attacker.example'}).status_code, 400)
        page = self.client.get('/')
        self.assertEqual(page.status_code, 200)
        self.assertIn('text/html', page.headers['content-type'])
        self.assertEqual(page.headers['x-content-type-options'], 'nosniff')


if __name__ == '__main__':
    unittest.main()
