"""Config loading for ~/.shop/config.yaml and ~/.shop/merchants.yaml.

Set SHOP_HOME env var to override the config directory (useful in read-only
home environments): SHOP_HOME=/writable/path shop search products "coffee"
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SHOP_DIR = Path(os.environ["SHOP_HOME"]) if "SHOP_HOME" in os.environ else Path.home() / ".shop"
CONFIG_PATH = SHOP_DIR / "config.yaml"
MERCHANTS_PATH = SHOP_DIR / "merchants.yaml"


@dataclass
class ShopConfig:
    default_mandate: str | None = None
    confidence_threshold: float = 0.80
    max_workers: int = 10


@dataclass
class MerchantConfig:
    slug: str
    name: str
    adapter: str  # "mock" | "ucp" | "shopify_storefront" | "stripe_demo"
    extra: dict = field(default_factory=dict)  # adapter-specific keys (ucp_endpoint, etc.)


def load_config(config_path: Path = CONFIG_PATH) -> ShopConfig:
    if not config_path.exists():
        return ShopConfig()
    with config_path.open() as f:
        data = yaml.safe_load(f) or {}
    return ShopConfig(
        default_mandate=data.get("default_mandate"),
        confidence_threshold=float(data.get("confidence_threshold", 0.80)),
        max_workers=int(data.get("max_workers", 10)),
    )


def load_merchants(merchants_path: Path = MERCHANTS_PATH) -> list[MerchantConfig]:
    """Load merchant list. Returns empty list if file missing."""
    if not merchants_path.exists():
        return []
    with merchants_path.open() as f:
        data = yaml.safe_load(f) or {}

    merchants = []
    for raw in data.get("merchants", []):
        slug = raw.get("slug") or raw.get("name", "unknown").lower().replace(" ", "-")
        extra = {k: v for k, v in raw.items() if k not in ("slug", "name", "adapter")}
        merchants.append(
            MerchantConfig(
                slug=slug,
                name=raw.get("name", slug),
                adapter=raw.get("adapter", "mock"),
                extra=extra,
            )
        )
    return merchants


def create_adapter(merchant: MerchantConfig):
    """Instantiate the correct adapter for a merchant config."""
    from shop.adapters.mock import MockAdapter
    from shop.adapters.ucp import UCPAdapter

    adapters = {
        "mock": MockAdapter,
        "ucp": UCPAdapter,
    }

    cls = adapters.get(merchant.adapter)
    if cls is None:
        raise ValueError(f"Unknown adapter type: {merchant.adapter!r} for merchant {merchant.slug!r}")

    return cls(merchant.slug, merchant.extra)
