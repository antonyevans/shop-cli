"""Tests for CommerceTXT normalization in MockAdapter.

Verifies that raw fixture records are correctly normalized to the
CommerceTXT format spec.
"""

from __future__ import annotations

import pytest

from shop.adapters.mock import MockAdapter
from shop.models.commerce import SearchFilters


@pytest.fixture
def adapter() -> MockAdapter:
    return MockAdapter(slug="mock", config={})


class TestSKUNamespacing:
    @pytest.mark.asyncio
    async def test_sku_is_namespaced(self, adapter):
        results = await adapter.search("coffee", SearchFilters())
        assert all(r.sku.startswith("mock:") for r in results)

    @pytest.mark.asyncio
    async def test_sku_format(self, adapter):
        results = await adapter.search("coffee", SearchFilters())
        assert results[0].sku == "mock:coffee-filters-100"


class TestNullFieldsPolicy:
    """Missing fields must be null (never omitted)."""

    @pytest.mark.asyncio
    async def test_missing_description_is_none_not_missing(self, adapter):
        # building-blocks-60pc has no description
        results = await adapter.search("building blocks", SearchFilters())
        assert len(results) >= 1
        blocks = next(r for r in results if "building-blocks" in r.sku)
        assert blocks.description is None  # present, but null

    @pytest.mark.asyncio
    async def test_missing_carrier_is_none(self, adapter):
        results = await adapter.search("spiral notebook", SearchFilters())
        notebook = next(r for r in results if "notebook" in r.sku)
        assert notebook.shipping.carrier is None

    @pytest.mark.asyncio
    async def test_missing_price_history_is_none(self, adapter):
        results = await adapter.search("building blocks", SearchFilters())
        blocks = next(r for r in results if "building-blocks" in r.sku)
        assert blocks.price_history_30d is None

    @pytest.mark.asyncio
    async def test_missing_stock_count_is_none(self, adapter):
        results = await adapter.search("widget", SearchFilters())
        widget = next(r for r in results if "widget" in r.sku)
        assert widget.stock_count is None

    @pytest.mark.asyncio
    async def test_missing_seller_rating_is_none(self, adapter):
        results = await adapter.search("widget", SearchFilters())
        widget = next(r for r in results if "widget" in r.sku)
        assert widget.trust.seller_rating is None

    @pytest.mark.asyncio
    async def test_missing_authenticity_is_none(self, adapter):
        results = await adapter.search("notebook", SearchFilters())
        notebook = next(r for r in results if "notebook" in r.sku)
        assert notebook.trust.authenticity is None


class TestAvailabilityMapping:
    @pytest.mark.asyncio
    async def test_in_stock_mapped(self, adapter):
        results = await adapter.search("coffee", SearchFilters())
        assert results[0].availability == "InStock"

    @pytest.mark.asyncio
    async def test_out_of_stock_mapped(self, adapter):
        results = await adapter.search("discontinued", SearchFilters())
        oos = next((r for r in results if "out-of-stock" in r.sku), None)
        assert oos is not None
        assert oos.availability == "OutOfStock"


class TestCachedAt:
    @pytest.mark.asyncio
    async def test_cached_at_is_present(self, adapter):
        results = await adapter.search("coffee", SearchFilters())
        assert all(r.cached_at for r in results)

    @pytest.mark.asyncio
    async def test_cached_at_is_iso8601(self, adapter):
        from datetime import datetime

        results = await adapter.search("coffee", SearchFilters())
        # Should parse without error
        datetime.fromisoformat(results[0].cached_at)


class TestTaxExcluded:
    @pytest.mark.asyncio
    async def test_tax_excluded_is_true(self, adapter):
        results = await adapter.search("coffee", SearchFilters())
        assert all(r.tax_excluded for r in results)


class TestPriceHistoryNormalization:
    @pytest.mark.asyncio
    async def test_price_history_has_min_max(self, adapter):
        results = await adapter.search("coffee", SearchFilters())
        history = results[0].price_history_30d
        assert history is not None
        assert "min" in history
        assert "max" in history
        assert isinstance(history["min"], float)
        assert isinstance(history["max"], float)
