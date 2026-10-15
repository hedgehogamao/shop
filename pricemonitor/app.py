"""Single-user, single-worker price monitor served on loopback by default."""
import asyncio
import contextlib
import logging
import json
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .extractor import PriceError, fetch_quote
from .store import Store

ROOT = Path(__file__).resolve().parent.parent
logger = logging.getLogger('pricemonitor')
CURRENCIES = {'CNY', 'USD', 'EUR', 'GBP', 'JPY', 'HKD', 'TWD', 'CAD', 'AUD', 'SGD'}


class ProductInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    url: str = Field(max_length=2048)
    name: str = Field(default='', max_length=200)
    selector: str = Field(default='', max_length=500)
    currency: str = 'CNY'
    target_price: str | None = None
    interval_minutes: int = Field(default=60, ge=15, le=10080)

    @field_validator('url')
    @classmethod
    def url_valid(cls, value):
        value = value.strip()
        try:
            parts = urlsplit(value)
            if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
                raise ValueError()
            if parts.port not in (None, 80, 443):
                raise ValueError()
            if any(ord(c) < 32 or c in '\\' for c in value):
                raise ValueError()
        except ValueError:
            raise ValueError('请输入不含账号密码的有效 HTTP/HTTPS 商品链接（端口限 80/443）。') from None
        return urlunsplit((parts.scheme, parts.netloc, parts.path or '/', parts.query, ''))

    @field_validator('name', 'selector')
    @classmethod
    def strip_text(cls, value):
        return value.strip()

    @field_validator('currency')
    @classmethod
    def currency_valid(cls, value):
        value = value.strip().upper()
        if value not in CURRENCIES:
            raise ValueError('请选择支持的货币。')
        return value

    @field_validator('target_price', mode='before')
    @classmethod
    def target_valid(cls, value):
        if value is None or value == '':
            return None
        try:
            amount = Decimal(str(value).strip())
            if not amount.is_finite() or amount < 0 or amount > Decimal('1000000000'):
                raise ValueError()
            if amount.as_tuple().exponent < -4:
                raise ValueError()
            return format(amount, 'f')
        except (InvalidOperation, ValueError):
            raise ValueError('目标价须为 0–10 亿之间的金额，最多 4 位小数。') from None


class ProductPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    url: str | None = None
    name: str | None = None
    selector: str | None = None
    currency: str | None = None
    target_price: str | None = None
    interval_minutes: int | None = None
    enabled: bool | None = None


class Checker:
    def __init__(self, store, fetcher=fetch_quote):
        self.store = store
        self.fetcher = fetcher
        self.running = {}
        self.semaphore = asyncio.Semaphore(4)

    async def check(self, product_id, scheduled=False):
        while True:
            task = self.running.get(product_id)
            if task is None:
                task = asyncio.create_task(self._perform(product_id, scheduled))
                self.running[product_id] = task

                def forget(completed, key=product_id):
                    if self.running.get(key) is completed:
                        self.running.pop(key, None)

                task.add_done_callback(forget)
            version, attempted = await asyncio.shield(task)
            if self.running.get(product_id) is task:
                self.running.pop(product_id, None)
            current = self.store.get(product_id)
            if current is None:
                return attempted
            if current['source_version'] != version or (not scheduled and not attempted):
                continue
            return attempted

    async def _perform(self, product_id, scheduled):
        async with self.semaphore:
            product = self.store.get(product_id)
            if not product:
                return None, False
            version = product['source_version']
            if scheduled and not product['enabled']:
                return version, False
            try:
                quote = await self.fetcher(product['url'], product['selector'], product['currency'])
                if quote.currency != product['currency']:
                    raise PriceError(f"页面货币为 {quote.currency}，与设置的 {product['currency']} 不一致。请编辑货币并重新填写目标价后检查。")
                self.store.result(product_id, version, quote=quote)
            except PriceError as exc:
                self.store.result(product_id, version, error=str(exc)[:500])
            except Exception:
                logger.exception('Price check failed for product %s', product_id)
                self.store.result(product_id, version, error='采集失败，请稍后重试。')
            return version, True

    async def close(self):
        tasks = list(self.running.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def due_once(self):
        timestamp = datetime.now(timezone.utc)
        due = []
        for product in self.store.list():
            last = product['last_checked']
            if product['enabled'] and (not last or (timestamp - datetime.fromisoformat(last)).total_seconds()
                                       >= product['interval_minutes'] * 60):
                due.append(product['id'])
        await asyncio.gather(*(self.check(item, scheduled=True) for item in due))

    async def loop(self):
        while True:
            try:
                await self.due_once()
            except Exception:
                logger.exception('Scheduler iteration failed')
            await asyncio.sleep(30)


def create_app(database_path=None, fetcher=fetch_quote, scheduler=True):
    @asynccontextmanager
    async def lifespan(application):
        store = Store(database_path or os.environ.get('SHOP_DB_PATH', ROOT / 'data' / 'prices.sqlite3'))
        application.state.store = store
        application.state.checker = Checker(store, fetcher)
        task = asyncio.create_task(application.state.checker.loop()) if scheduler else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await application.state.checker.close()

    application = FastAPI(title='双十一装机价格监测', version='0.1.0', lifespan=lifespan,
                          docs_url=None, redoc_url=None)
    allowed_hosts = os.environ.get('SHOP_ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=[h.strip() for h in allowed_hosts if h.strip()])

    @application.middleware('http')
    async def browser_protection(request: Request, call_next):
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('origin')
            expected = f'{request.url.scheme}://{request.url.netloc}'
            if request.headers.get('sec-fetch-site') == 'cross-site' or (origin and origin != expected):
                return JSONResponse({'detail': '请从监测页面操作。'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"
        return response

    @application.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        messages = ['.'.join(str(x) for x in e['loc'][1:]) + ': ' + e['msg'].removeprefix('Value error, ')
                    for e in exc.errors()]
        return JSONResponse({'detail': '；'.join(messages)}, status_code=422)

    def get_store(request):
        return request.app.state.store

    @application.get('/api/health')
    async def health(request: Request):
        get_store(request).list()  # Verify the persistent store is readable.
        return {'status': 'ok', 'scheduler': scheduler, 'version': '0.1.0'}

    @application.get('/api/products')
    async def products(request: Request):
        return get_store(request).list()

    @application.get('/api/build-reference')
    async def build_reference():
        return json.loads((ROOT / 'config' / 'build-reference.json').read_text(encoding='utf-8'))

    @application.post('/api/products', status_code=201)
    async def add_product(payload: ProductInput, request: Request):
        store = get_store(request)
        try:
            product_id = store.create(payload.model_dump())
        except sqlite3.IntegrityError:
            raise HTTPException(409, '该商品链接已经在监测列表中。') from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        await request.app.state.checker.check(product_id)
        return store.get(product_id)

    @application.patch('/api/products/{product_id}')
    async def edit_product(product_id: str, payload: ProductPatch, request: Request):
        store = get_store(request)
        existing = store.get(product_id)
        if not existing:
            raise HTTPException(404, '未找到这个监测项。')
        fields = payload.model_dump(exclude_unset=True)
        if ('currency' in fields and fields['currency'] is not None
                and fields['currency'].strip().upper() != existing['currency'] and 'target_price' not in fields):
            fields['target_price'] = None
        # Validate merged fields with the same rules as creation, including null handling.
        raw = {key: existing[key] for key in ProductInput.model_fields}
        raw.update({key: value for key, value in fields.items() if key != 'enabled'})
        try:
            valid = ProductInput.model_validate(raw).model_dump()
        except Exception as exc:
            from pydantic import ValidationError
            if isinstance(exc, ValidationError):
                raise HTTPException(422, '；'.join(e['msg'].removeprefix('Value error, ') for e in exc.errors())) from None
            raise
        if fields.get('enabled', True) is None:
            raise HTTPException(422, 'enabled 不能为 null。')
        fields = {key: valid[key] if key != 'enabled' else value for key, value in fields.items()}
        try:
            result = store.update(product_id, fields)
        except sqlite3.IntegrityError:
            raise HTTPException(409, '该商品链接已经在监测列表中。') from None
        return result

    @application.delete('/api/products/{product_id}', status_code=204)
    async def delete_product(product_id: str, request: Request):
        if not get_store(request).delete(product_id):
            raise HTTPException(404, '未找到这个监测项。')

    @application.post('/api/products/{product_id}/check')
    async def check_product(product_id: str, request: Request):
        store = get_store(request)
        if not store.get(product_id):
            raise HTTPException(404, '未找到这个监测项。')
        await request.app.state.checker.check(product_id)
        result = store.get(product_id)
        if result is None:
            raise HTTPException(404, '该监测项已被删除。')
        return result

    @application.post('/api/check-all')
    async def check_all(request: Request):
        ids = [p['id'] for p in get_store(request).list() if p['enabled']]
        results = await asyncio.gather(*(request.app.state.checker.check(p, scheduled=True) for p in ids))
        return {'checked': sum(results)}

    @application.get('/')
    async def index():
        return FileResponse(ROOT / 'static' / 'index.html')

    application.mount('/static', StaticFiles(directory=ROOT / 'static', check_dir=False), name='static')
    return application


app = create_app()
