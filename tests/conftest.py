"""Shared test fixtures."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from shop.config import MerchantConfig, ShopConfig
from shop.models.commerce import (
    CommerceTXTProduct,
    CommerceTXTReturns,
    CommerceTXTShipping,
    CommerceTXTTrust,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def full_product() -> CommerceTXTProduct:
    """High-quality product — all fields present, great ratings."""
    return CommerceTXTProduct(
        sku="mock:coffee-filters-100",
        title="Premium Cone Coffee Filters, 100ct",
        description="Unbleached cone filters.",
        price=8.99,
        price_history_30d={"min": 8.99, "max": 9.49},
        availability="InStock",
        stock_count=847,
        shipping=CommerceTXTShipping(cost=0.00, window_days="2-4", carrier="USPS"),
        returns=CommerceTXTReturns(
            window_days=30, restocking_fee=None, condition="unopened", refund_timeline_days=5
        ),
        trust=CommerceTXTTrust(
            seller_rating=4.7, review_count=312, certifications=[], authenticity="verified"
        ),
        cached_at=_now(),
    )


@pytest.fixture
def minimal_product() -> CommerceTXTProduct:
    """Bare-minimum product — only required fields, everything else null."""
    return CommerceTXTProduct(
        sku="mock:widget-x",
        title="Generic Widget",
        price=3.49,
        availability="InStock",
        shipping=CommerceTXTShipping(cost=None, window_days=None),  # missing shipping fields
        returns=CommerceTXTReturns(),
        trust=CommerceTXTTrust(seller_rating=None, review_count=3),
        cached_at=_now(),
    )


@pytest.fixture
def mock_merchant() -> MerchantConfig:
    return MerchantConfig(slug="mock", name="Mock Store", adapter="mock")


@pytest.fixture
def default_config() -> ShopConfig:
    return ShopConfig(confidence_threshold=0.80, max_workers=10)


@pytest.fixture
def low_threshold_config() -> ShopConfig:
    """Config with 0.0 threshold — all results pass."""
    return ShopConfig(confidence_threshold=0.0, max_workers=10)
