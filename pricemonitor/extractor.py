"""Extract a single, explicit product price from a public HTML page.

DNS checks reject non-public targets and run again for every redirect. A proxy
may resolve DNS independently, so deployment network rules must also restrict
access to private destinations; these checks do not pin the connection's IP.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup, Tag, UnicodeDammit


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5


class PriceError(ValueError):
    """The page does not provide one trustworthy, unambiguous price."""


@dataclass(frozen=True)
class Quote:
    name: str
    price: str
    currency: str


def _currency(value: object, fallback: str = "CNY") -> str:
    text = str(value).strip().upper() if value is not None else ""
    aliases = {
        "RMB": "CNY", "人民币": "CNY", "￥": "CNY", "元": "CNY",
        "$": "USD", "US$": "USD", "美元": "USD", "€": "EUR",
        "欧元": "EUR", "£": "GBP", "英镑": "GBP", "日元": "JPY",
    }
    if text == "¥":
        return "JPY" if fallback == "JPY" else "CNY"
    text = aliases.get(text, text)
    if not re.fullmatch(r"[A-Z]{3}", text):
        raise PriceError("无法识别价格币种，请指定币种。")
    return text


_PRICE_TEXT = re.compile(
    r"^\s*(?P<prefix>US\$|[A-Za-z]{3}|人民币|美元|欧元|英镑|日元|[￥¥$€£])?"
    r"\s*(?P<amount>[0-9]+(?:[.,][0-9]+)*)\s*"
    r"(?P<suffix>[A-Za-z]{3}|人民币|美元|欧元|英镑|日元|元|[￥¥$€£])?\s*$"
)


def _amount(value: object, currency: object = None, fallback: str = "CNY") -> tuple[str, str]:
    if isinstance(value, bool) or value is None:
        raise PriceError("价格格式无效。")
    text = format(value, "f") if isinstance(value, Decimal) else str(value).strip()
    match = _PRICE_TEXT.fullmatch(text)
    if not match:
        raise PriceError("价格必须是一个非负数；包含多个价格时请指定更精确的选择器。")
    fallback = _currency(fallback)
    explicit = _currency(currency, fallback) if currency is not None and str(currency).strip() else None
    embedded = [_currency(token, explicit or fallback) for token in (match["prefix"], match["suffix"]) if token]
    # The yen sign is shared by CNY and JPY; an explicit site currency wins.
    if embedded and (len(set(embedded)) != 1 or explicit and any(code != explicit for code in embedded)):
        raise PriceError("价格的币种标记互相冲突。")
    selected_currency = explicit or (embedded[0] if embedded else fallback)
    amount = match["amount"]
    if "." in amount and "," in amount:
        decimal_separator = "." if amount.rfind(".") > amount.rfind(",") else ","
        grouping_separator = "," if decimal_separator == "." else "."
        whole, fraction = amount.rsplit(decimal_separator, 1)
        if decimal_separator in whole or not re.fullmatch(r"[0-9]{1,3}(?:" + re.escape(grouping_separator) + r"[0-9]{3})+", whole):
            raise PriceError("价格的千位分隔符格式无效。")
        amount = whole.replace(grouping_separator, "") + "." + fraction
    elif "," in amount:
        groups = amount.split(",")
        if len(groups) == 2 and len(groups[1]) != 3:
            amount = ".".join(groups)
        elif len(groups[0]) in (1, 2, 3) and all(len(group) == 3 for group in groups[1:]):
            amount = "".join(groups)
        else:
            raise PriceError("价格的小数或千位分隔符格式无效。")
    elif amount.count(".") > 1:
        groups = amount.split(".")
        if len(groups[0]) in (1, 2, 3) and all(len(group) == 3 for group in groups[1:]):
            amount = "".join(groups)
        else:
            raise PriceError("价格的小数或千位分隔符格式无效。")
    try:
        number = Decimal(amount)
    except InvalidOperation as exc:
        raise PriceError("价格格式无效。") from exc
    if not number.is_finite() or number < 0:
        raise PriceError("价格必须是有限的非负数。")
    normalized = format(number, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized, selected_currency


def _types(node: dict) -> set[str]:
    value = node.get("@type", [])
    values = value if isinstance(value, list) else [value]
    return {str(item).rsplit("/", 1)[-1].rsplit("#", 1)[-1] for item in values}


def _products(value: object):
    if isinstance(value, dict):
        if "Product" in _types(value):
            yield value
        for child in value.values():
            yield from _products(child)
    elif isinstance(value, list):
        for child in value:
            yield from _products(child)


def _not_expired(offer: dict) -> None:
    valid_until = offer.get("priceValidUntil")
    if not valid_until:
        return
    try:
        expiry = date.fromisoformat(str(valid_until)[:10])
    except ValueError as exc:
        raise PriceError("网页报价有效期格式无效。") from exc
    if expiry < datetime.now(timezone.utc).date():
        raise PriceError("网页中的报价已经过期，请指定当前价格位置。")


def _offers(value: object, fallback: str) -> tuple[str, str, bool] | None:
    offers = value if isinstance(value, list) else [value]
    candidates: list[tuple[str, str, bool]] = []
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        _not_expired(offer)
        aggregate = "AggregateOffer" in _types(offer)
        raw_price = offer.get("lowPrice") if aggregate and offer.get("lowPrice") is not None else offer.get("price")
        if raw_price is None:
            if offer.get("offers") is not None:
                nested = _offers(offer["offers"], fallback)
                if nested:
                    candidates.append(nested)
            continue
        price, currency = _amount(raw_price, offer.get("priceCurrency"), fallback)
        is_minimum = False
        if aggregate:
            if offer.get("highPrice") is not None:
                high, high_currency = _amount(offer["highPrice"], offer.get("priceCurrency"), fallback)
                if high_currency != currency or Decimal(high) < Decimal(price):
                    raise PriceError("网页最低和最高报价不一致。")
                is_minimum = Decimal(high) != Decimal(price)
            else:
                is_minimum = True
        candidates.append((price, currency, is_minimum))
    if not candidates:
        return None
    if len({(price, currency) for price, currency, _ in candidates}) != 1:
        raise PriceError("网页包含多个规格或币种的报价，请指定价格选择器。")
    price, currency, _ = candidates[0]
    return price, currency, any(minimum for _, _, minimum in candidates)


def _title(soup: BeautifulSoup) -> str:
    node = soup.find("meta", attrs={"property": "og:title"})
    if node and node.get("content"):
        return str(node["content"]).strip()[:500]
    return soup.title.get_text(" ", strip=True)[:500] if soup.title else "商品"


def _node_value(node: Tag) -> str:
    for attribute in ("content", "data-price", "value"):
        if node.get(attribute) is not None:
            return str(node[attribute]).strip()
    return node.get_text(" ", strip=True)


def _meta_values(soup: BeautifulSoup, key: str) -> list[str]:
    nodes = soup.find_all("meta", attrs={"property": key}) + soup.find_all("meta", attrs={"name": key})
    return [str(node["content"]).strip() for node in nodes if node.get("content") is not None]


def _page_currency(soup: BeautifulSoup, fallback: str) -> str | None:
    values = _meta_values(soup, "product:price:currency")
    if not values:
        values = [_node_value(node) for node in soup.select('[itemprop~="priceCurrency"]')]
    currencies = {_currency(value, fallback) for value in values if value}
    if len(currencies) > 1:
        raise PriceError("网页包含多个币种，请缩小价格选择器范围。")
    return currencies.pop() if currencies else None


def extract_price(html: str, selector: str = "", fallback_currency: str = "CNY") -> Quote:
    """Prefer explicit product data; never guess a number from full-page text."""
    fallback = _currency(fallback_currency)
    soup = BeautifulSoup(html, "html.parser")
    title = _title(soup)
    if selector.strip():
        try:
            nodes = soup.select(selector.strip())
        except Exception as exc:
            raise PriceError("价格 CSS 选择器格式无效。") from exc
        if len(nodes) != 1:
            raise PriceError("价格选择器必须匹配一个元素，请使用更精确的选择器。")
        node = nodes[0]
        currency = node.get("data-currency")
        if not currency:
            # A nearby microdata scope is more specific than page-wide metadata.
            scope = node.find_parent(attrs={"itemscope": True})
            local = scope.select('[itemprop~="priceCurrency"]') if scope else []
            currency = _node_value(local[0]) if len(local) == 1 else _page_currency(soup, fallback)
        price, code = _amount(_node_value(node), currency, fallback)
        return Quote(title, price, code)

    quotes: list[tuple[str, Quote]] = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"^application/ld\+json(?:\s*;.*)?$", re.I)}):
        try:
            value = json.loads(script.string or script.get_text(), parse_float=Decimal)
        except (ValueError, TypeError):
            continue
        for product in _products(value):
            candidate = _offers(product.get("offers"), fallback)
            if not candidate:
                continue
            price, currency, minimum = candidate
            name = str(product.get("name") or title).strip()[:500]
            identity = str(product.get("@id") or product.get("url") or product.get("name") or id(product))
            if minimum:
                name += "（最低报价）"
            quotes.append((identity, Quote(name, price, currency)))
    if quotes:
        if len({(identity, quote.price, quote.currency, quote.name) for identity, quote in quotes}) != 1:
            raise PriceError("网页包含多个商品的报价，请指定要监测的价格选择器。")
        return quotes[0][1]

    amounts = _meta_values(soup, "product:price:amount")
    if amounts:
        currency = _page_currency(soup, fallback)
        candidates = {_amount(value, currency, fallback) for value in amounts}
        if len(candidates) != 1:
            raise PriceError("网页包含多个价格，请指定价格选择器。")
        price, code = candidates.pop()
        return Quote(title, price, code)

    price_nodes = soup.select('[itemprop~="price"]')
    if price_nodes:
        scopes = {
            id(scope)
            for node in price_nodes
            if (scope := node.find_parent(attrs={"itemtype": re.compile(r"(?:/|#)Product(?:\s|$)")})) is not None
        }
        if len(scopes) > 1:
            raise PriceError("网页包含多个商品的报价，请指定价格选择器。")
        currency = _page_currency(soup, fallback)
        candidates = {_amount(_node_value(node), currency, fallback) for node in price_nodes}
        if len(candidates) != 1:
            raise PriceError("网页包含多个规格的报价，请指定价格选择器。")
        price, code = candidates.pop()
        return Quote(title, price, code)
    raise PriceError("网页没有可识别的商品价格，请提供价格 CSS 选择器；动态页面可能需要商家接口。")


def _public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_global and not (
        address.is_private or address.is_loopback or address.is_link_local
        or address.is_multicast or address.is_reserved or address.is_unspecified
    )


async def validate_url(url: str) -> str:
    """Normalize an HTTP(S) URL and verify every resolved address is public."""
    if not isinstance(url, str) or not url or re.search(r"[\x00-\x20\x7f\\]", url):
        raise PriceError("网址格式无效。")
    try:
        parts = urlsplit(url)
        port = parts.port
        host = parts.hostname
    except ValueError as exc:
        raise PriceError("网址格式无效。") from exc
    if parts.scheme.lower() not in ("http", "https") or not host:
        raise PriceError("只支持完整的 http 或 https 商品网址。")
    if parts.username is not None or parts.password is not None:
        raise PriceError("商品网址不能包含账户密码。")
    if port not in (None, 80, 443):
        raise PriceError("商品网址只支持 80 或 443 端口。")
    host = host.rstrip(".").lower()
    if not host or "%" in host or host == "localhost" or host.endswith((".localhost", ".local")):
        raise PriceError("只允许访问公网商品网址。")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise PriceError("网址域名格式无效。") from exc
        if len(host) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split(".")):
            raise PriceError("网址域名格式无效。")
    if address is not None:
        if not _public_address(str(address)):
            raise PriceError("只允许访问公网商品网址。")
        host = str(address)
    else:
        try:
            resolved = await asyncio.wait_for(
                asyncio.to_thread(socket.getaddrinfo, host, port or (443 if parts.scheme.lower() == "https" else 80), type=socket.SOCK_STREAM),
                timeout=10,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise PriceError("无法解析商品网址的公网地址。") from exc
        if not resolved or any(not _public_address(record[4][0]) for record in resolved):
            raise PriceError("只允许访问公网商品网址。")
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        if port < 1:
            raise PriceError("网址端口无效。")
        authority += f":{port}"
    return urlunsplit((parts.scheme.lower(), authority, parts.path or "/", parts.query, ""))


async def fetch_quote(url: str, selector: str = "", fallback_currency: str = "CNY") -> Quote:
    """Fetch bounded HTML with verified TLS and validate every redirect target."""
    current = await validate_url(url)
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20.0), follow_redirects=False, trust_env=True,
            headers={"User-Agent": "PriceMonitor/0.1", "Accept": "text/html,application/xhtml+xml"},
        ) as client:
            for redirect_count in range(MAX_REDIRECTS + 1):
                async with client.stream("GET", current) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        if redirect_count == MAX_REDIRECTS:
                            raise PriceError("商品网址重定向次数过多。")
                        location = response.headers.get("location")
                        if not location:
                            raise PriceError("商品网页重定向缺少目标网址。")
                        if re.search(r"[\x00-\x20\x7f\\]", location):
                            raise PriceError("商品网页重定向目标无效。")
                        current = await validate_url(urljoin(current, location))
                        continue
                    if not 200 <= response.status_code < 300:
                        raise PriceError(f"商家网页返回 HTTP {response.status_code}，暂时无法读取价格。")
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if content_type and content_type not in ("text/html", "application/xhtml+xml"):
                        raise PriceError("商品网址没有返回 HTML 网页。")
                    try:
                        content_length = int(response.headers.get("content-length", "0"))
                    except ValueError:
                        content_length = 0
                    if content_length > MAX_RESPONSE_BYTES:
                        raise PriceError("商品网页超过 2 MB，无法读取。")
                    chunks = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            raise PriceError("商品网页超过 2 MB，无法读取。")
                        chunks.append(chunk)
                    content = b"".join(chunks)
                    charset = response.charset_encoding
                    if charset:
                        try:
                            html = content.decode(charset, errors="replace")
                        except LookupError:
                            html = content.decode("utf-8", errors="replace")
                    else:
                        html = UnicodeDammit(content, is_html=True).unicode_markup or ""
                    return extract_price(html, selector, fallback_currency)
    except httpx.TimeoutException as exc:
        raise PriceError("读取商品网页超时，请稍后重试。") from exc
    except httpx.RequestError as exc:
        raise PriceError("无法连接商品网页，请检查网址或商家访问限制。") from exc
    raise PriceError("未能读取商品价格。")
