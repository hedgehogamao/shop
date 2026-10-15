"""SQLite persistence. Monetary values remain decimal strings throughout."""
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS products (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, auto_name INTEGER NOT NULL,
                    url TEXT NOT NULL UNIQUE, selector TEXT NOT NULL DEFAULT '',
                    currency TEXT NOT NULL, target_price TEXT, interval_minutes INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, source_version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, last_checked TEXT, last_success TEXT, error TEXT
                );
                CREATE TABLE IF NOT EXISTS prices (
                    id INTEGER PRIMARY KEY, product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                    source_version INTEGER NOT NULL, price TEXT NOT NULL, currency TEXT NOT NULL,
                    checked_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS prices_product_time ON prices(product_id, source_version, id);
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _serialize(self, row, db):
        item = dict(row)
        item['enabled'] = bool(item['enabled'])
        records = db.execute(
            'SELECT price,currency,checked_at FROM prices WHERE product_id=? AND source_version=? ORDER BY id DESC LIMIT 200',
            (item['id'], item['source_version']),
        ).fetchall()
        item['history'] = [dict(r) for r in reversed(records)]
        item['latest_price'] = records[0]['price'] if records else None
        item['previous_price'] = records[1]['price'] if len(records) > 1 else None
        fresh = False
        if item['last_success']:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(item['last_success'])).total_seconds()
            fresh = age <= item['interval_minutes'] * 120
        item['alert'] = bool(fresh and not item['error'] and item['target_price'] is not None
                             and item['latest_price'] is not None
                             and Decimal(item['latest_price']) <= Decimal(item['target_price']))
        return item

    def list(self):
        with closing(self.connect()) as db:
            return [self._serialize(r, db) for r in db.execute('SELECT * FROM products ORDER BY created_at DESC')]

    def get(self, product_id):
        with closing(self.connect()) as db:
            row = db.execute('SELECT * FROM products WHERE id=?', (product_id,)).fetchone()
            return self._serialize(row, db) if row else None

    def create(self, fields):
        product_id = uuid4().hex
        values = {**fields, 'id': product_id, 'created_at': now(), 'auto_name': int(not fields['name'])}
        if not values['name']:
            from urllib.parse import urlsplit
            values['name'] = urlsplit(values['url']).hostname
        with closing(self.connect()) as db, db:
            # The cap and insert share one write transaction, including concurrent requests.
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT count(*) FROM products').fetchone()[0] >= 100:
                raise ValueError('最多监测 100 个商品，请先移除不再需要的监测项。')
            db.execute('''INSERT INTO products
                (id,name,auto_name,url,selector,currency,target_price,interval_minutes,created_at)
                VALUES (:id,:name,:auto_name,:url,:selector,:currency,:target_price,:interval_minutes,:created_at)''', values)
        return product_id

    def update(self, product_id, fields):
        if not fields:
            return self.get(product_id)
        with closing(self.connect()) as db, db:
            row = db.execute('SELECT * FROM products WHERE id=?', (product_id,)).fetchone()
            if row is None:
                return None
            changed_source = any(k in fields and fields[k] != row[k] for k in ('url','currency','selector'))
            if 'name' in fields:
                fields['auto_name'] = int(not fields['name'])
                if not fields['name']:
                    from urllib.parse import urlsplit
                    fields['name'] = urlsplit(fields.get('url', row['url'])).hostname
            if changed_source:
                fields.update(source_version=row['source_version'] + 1,
                              last_checked=None, last_success=None, error=None)
            sql = ','.join(f'{key}=?' for key in fields)
            db.execute(f'UPDATE products SET {sql} WHERE id=?', (*fields.values(), product_id))
        return self.get(product_id)

    def result(self, product_id, version, quote=None, error=None):
        timestamp = now()
        with closing(self.connect()) as db, db:
            row = db.execute('SELECT * FROM products WHERE id=? AND source_version=?', (product_id, version)).fetchone()
            if row is None:
                return  # A deleted or reconfigured target cannot receive an old in-flight result.
            db.execute('UPDATE products SET last_checked=?, error=? WHERE id=?', (timestamp, error, product_id))
            if quote is not None:
                db.execute('INSERT INTO prices(product_id,source_version,price,currency,checked_at) VALUES(?,?,?,?,?)',
                           (product_id, version, quote.price, quote.currency, timestamp))
                db.execute('UPDATE products SET last_success=? WHERE id=?', (timestamp, product_id))
                if row['auto_name']:
                    db.execute('UPDATE products SET name=? WHERE id=?', (quote.name[:200], product_id))

    def delete(self, product_id):
        with closing(self.connect()) as db, db:
            return bool(db.execute('DELETE FROM products WHERE id=?', (product_id,)).rowcount)
