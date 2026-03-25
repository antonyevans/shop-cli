"""SQLite DB layer for shop-cli. Single file: {shop_dir}/shop.db."""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id        TEXT PRIMARY KEY,
    timestamp       INTEGER NOT NULL,
    sku             TEXT NOT NULL,
    merchant        TEXT NOT NULL,
    price_usd       REAL NOT NULL,
    mandate_id      TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    exit_code       INTEGER NOT NULL,
    idempotency_key TEXT UNIQUE,
    raw_response    TEXT
);
CREATE TABLE IF NOT EXISTS cart_items (
    session_id     TEXT NOT NULL,
    sku            TEXT NOT NULL,
    merchant       TEXT NOT NULL,
    quantity       INTEGER DEFAULT 1,
    price_usd      REAL NOT NULL,
    added_at       INTEGER NOT NULL,
    PRIMARY KEY (session_id, sku)
);
CREATE TABLE IF NOT EXISTS mandate_spend (
    mandate_id     TEXT NOT NULL,
    order_id       TEXT NOT NULL REFERENCES orders(order_id),
    amount_usd     REAL NOT NULL,
    category       TEXT,
    recorded_at    INTEGER NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending'
);
"""


def get_db(shop_dir: Path) -> sqlite3.Connection:
    shop_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(shop_dir / "shop.db"))
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn
