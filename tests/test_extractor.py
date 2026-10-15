import gzip
import json
import socket
import unittest
from unittest.mock import patch

import httpx

from pricemonitor import extractor
from pricemonitor.extractor import PriceError, Quote, extract_price, fetch_quote, validate_url


def product_html(offers, name="显卡", **extra):
    product = {"@type": "Product", "name": name, "offers": offers, **extra}
    return '<script type="application/ld+json">' + json.dumps(product) + '</script>'


def offer(price="1299.50", currency="CNY", **extra):
    return {"@type": "Offer", "price": price, "priceCurrency": currency, **extra}


class ExtractPriceTests(unittest.TestCase):
    def test_nested_graph_and_exact_decimal(self):
        html = '<script type="application/ld+json">{"@graph":[{"@type":"Product","name":"CPU","offers":{"@type":"Offer","price":1299.50,"priceCurrency":"CNY"}}]}</script>'
        self.assertEqual(extract_price(html), Quote("CPU", "1299.5", "CNY"))

    def test_decimal_formats_and_currency_symbols(self):
        for raw, expected, currency in [
            ("¥1,299.50", "1299.5", "CNY"),
            ("€1.299,50", "1299.5", "EUR"),
            ("1299,50", "1299.5", "CNY"),
            ("$10", "10", "USD"),
            ("£0.00", "0", "GBP"),
            ("RMB 123", "123", "CNY"),
            ("1,299", "1299", "CNY"),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(extract_price(f'<b id="price">{raw}</b>', "#price"), Quote("商品", expected, currency))

    def test_selector_content_and_page_currency(self):
        html = '<meta property="og:title" content="内存"><meta property="product:price:currency" content="USD"><span id="price" content="29.95">unrelated</span>'
        self.assertEqual(extract_price(html, "#price"), Quote("内存", "29.95", "USD"))

    def test_explicit_currency_conflicts_rejected(self):
        html = '<meta property="product:price:currency" content="CNY"><b id="p">$10</b>'
        with self.assertRaises(PriceError):
            extract_price(html, "#p")

    def test_site_currency_overrides_fallback(self):
        self.assertEqual(extract_price(product_html(offer("19.99", "usd"))).currency, "USD")
        html = '<meta property="product:price:amount" content="$19.99">'
        self.assertEqual(extract_price(html).currency, "USD")
        html = '<meta itemprop="priceCurrency" content="JPY"><meta itemprop="price" content="¥1299">'
        self.assertEqual(extract_price(html).currency, "JPY")

    def test_aggregate_is_explicitly_minimum(self):
        html = product_html({"@type": "AggregateOffer", "lowPrice": "100", "highPrice": "200", "priceCurrency": "CNY"})
        self.assertEqual(extract_price(html), Quote("显卡（最低报价）", "100", "CNY"))

    def test_equal_duplicate_offers_and_zero_allowed(self):
        self.assertEqual(extract_price(product_html([offer("0"), offer(0)])).price, "0")

    def test_distinct_variant_prices_or_currencies_rejected(self):
        for offers in [[offer("100"), offer("200")], [offer("100", "CNY"), offer("100", "USD")]]:
            with self.subTest(offers=offers), self.assertRaises(PriceError):
                extract_price(product_html(offers))

    def test_recommendations_do_not_choose_arbitrary_product(self):
        html = product_html(offer(), "CPU") + product_html(offer(), "推荐显卡")
        with self.assertRaises(PriceError):
            extract_price(html)

    def test_missing_or_expired_price_never_guessed(self):
        for html in [
            '<title>CPU 9999</title><p>剩余 100 件；运费 20 元</p>',
            product_html(offer("100", priceValidUntil="2000-01-01")),
            product_html({"@type": "Offer", "shippingDetails": {"price": "20"}}),
        ]:
            with self.subTest(html=html), self.assertRaises(PriceError):
                extract_price(html)

    def test_multiple_price_text_and_invalid_numbers_rejected(self):
        for raw in ["100 200", "¥100 原价¥200", "-1", "NaN", "Infinity", "1,23,45", "1.2.3", "10-20"]:
            with self.subTest(raw=raw), self.assertRaises(PriceError):
                extract_price(f'<span id="p">{raw}</span>', "#p")
        with self.assertRaises(PriceError):
            extract_price(product_html(offer(True)))

    def test_selector_must_be_unique(self):
        for selector in ["b", "#missing", "["]:
            with self.subTest(selector=selector), self.assertRaises(PriceError):
                extract_price('<b>1</b><b>2</b>', selector)

    def test_microdata_multiple_products_rejected(self):
        html = '<div itemscope itemtype="https://schema.org/Product"><span itemprop="price">10</span></div>'
        with self.assertRaises(PriceError):
            extract_price(html + html)


PUBLIC_DNS = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


class URLSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_urls_and_private_addresses(self):
        for url in [
            "file:///etc/passwd", "ftp://example.com/a", "https:///no-host",
            "https://user:password@example.com", "https://example.com:8080",
            "http://localhost./", "http://x.localhost/", "http://x.local/",
            "http://127.0.0.1/", "http://10.0.0.1/", "http://169.254.169.254/",
            "http://100.64.0.1/", "http://224.0.0.1/", "http://[::1]/",
            "http://[fc00::1]/", "http://[fe80::1]/", "http://[ff02::1]/",
            "http://[::ffff:127.0.0.1]/", "http://[::ffff:10.0.0.1]/",
            "http://example.com\\@127.0.0.1/", "http://example.com/\n",
        ]:
            with self.subTest(url=url), self.assertRaises(PriceError):
                await validate_url(url)

    async def test_reject_any_private_dns_answer(self):
        private = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0))
        with patch.object(extractor.socket, "getaddrinfo", return_value=PUBLIC_DNS + [private]):
            with self.assertRaises(PriceError):
                await validate_url("https://shop.example/gpu")

    async def test_alternative_loopback_encoding_rejected_after_dns(self):
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
        with patch.object(extractor.socket, "getaddrinfo", return_value=private):
            for host in ["127.1", "2130706433", "0x7f000001"]:
                with self.subTest(host=host), self.assertRaises(PriceError):
                    await validate_url("http://" + host)

    async def test_public_normalization_and_dns_failure(self):
        with patch.object(extractor.socket, "getaddrinfo", return_value=PUBLIC_DNS):
            self.assertEqual(await validate_url("HTTPS://SHOP.EXAMPLE./gpu?q=1#price"), "https://shop.example/gpu?q=1")
        with patch.object(extractor.socket, "getaddrinfo", side_effect=socket.gaierror()):
            with self.assertRaises(PriceError):
                await validate_url("https://shop.example")


class FetchSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def run_fetch(self, handler):
        original_client = httpx.AsyncClient
        client_arguments = []

        def make_client(**kwargs):
            client_arguments.append(kwargs.copy())
            return original_client(transport=httpx.MockTransport(handler), **kwargs)

        with patch.object(extractor.socket, "getaddrinfo", return_value=PUBLIC_DNS) as dns:
            with patch.object(extractor.httpx, "AsyncClient", side_effect=make_client):
                result = await fetch_quote("https://shop.example/gpu")
        self.assertTrue(client_arguments[0]["trust_env"])
        self.assertNotIn("verify", client_arguments[0])
        return result, dns.call_count

    async def test_relative_redirect_revalidates_host(self):
        requested = []

        def handler(request):
            requested.append(str(request.url))
            if request.url.path == "/gpu":
                return httpx.Response(302, headers={"location": "/current"})
            return httpx.Response(200, headers={"content-type": "text/html"}, text=product_html(offer()))

        quote, dns_count = await self.run_fetch(handler)
        self.assertEqual(quote.price, "1299.5")
        self.assertEqual(dns_count, 2)
        self.assertEqual(len(requested), 2)

    async def test_private_redirect_never_requested(self):
        for target in ["http://127.0.0.1/a", "//169.254.169.254/latest", "https://shop.example:8080/a"]:
            requests = []

            def handler(request):
                requests.append(request)
                return httpx.Response(302, headers={"location": target})

            with self.subTest(target=target), self.assertRaises(PriceError):
                await self.run_fetch(handler)
            self.assertEqual(len(requests), 1)

    async def test_redirect_limit(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(302, headers={"location": "/loop"})

        with self.assertRaises(PriceError):
            await self.run_fetch(handler)
        self.assertEqual(len(requests), extractor.MAX_REDIRECTS + 1)

    async def test_reject_oversize_decoded_body_even_if_length_is_small(self):
        raw = b"x" * (extractor.MAX_RESPONSE_BYTES + 1)
        compressed = gzip.compress(raw)

        def handler(request):
            return httpx.Response(200, headers={"content-type": "text/html", "content-encoding": "gzip", "content-length": str(len(compressed))}, content=compressed)

        with self.assertRaises(PriceError):
            await self.run_fetch(handler)

    async def test_blocked_or_non_html_response_never_yields_price(self):
        for status, headers in [(403, {}), (200, {"content-type": "application/json"})]:
            with self.subTest(status=status), self.assertRaises(PriceError):
                await self.run_fetch(lambda request: httpx.Response(status, headers=headers, text=product_html(offer())))


if __name__ == "__main__":
    unittest.main()
