"""Tests for ShopifyCatalogAdapter — Shopify Global Catalog search."""

from __future__ import annotations

import pytest
import respx
import httpx

from shop.adapters.shopify_catalog import ShopifyCatalogAdapter, _TOKEN_URL, _SEARCH_URL
from shop.adapters.base import AdapterError, CheckoutNotSupportedError, ProductNotFoundError
from shop.models.commerce import SearchFilters


def _make_adapter(client_id: str = "cid", client_secret: str = "csec") -> ShopifyCatalogAdapter:
    return ShopifyCatalogAdapter(
        slug="shopify",
        config={"client_id": client_id, "client_secret": client_secret, "ships_to": "US"},
    )


def _token_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={"access_token": "shpat_test", "scope": "read_global_api_catalog_search", "expires_in": 3600},
    )


def _search_response(products: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"products": products})


_SAMPLE_PRODUCT = {
    "upid": "abc123",
    "title": "Arabica Coffee Filters 100-pack",
    "description": "Paper filters for drip coffee",
    "vendor": "FilterCo",
    "variants": [{"price": "12.99", "available": True}],
    "tags": [],
}


class TestTokenRefresh:
    @pytest.mark.asyncio
    async def test_token_fetched_on_first_search(self):
        adapter = _make_adapter()
        with respx.mock:
            respx.post(_TOKEN_URL).mock(return_value=_token_response())
            respx.get(_SEARCH_URL).mock(return_value=_search_response([_SAMPLE_PRODUCT]))
            results = await adapter.search("coffee", SearchFilters())
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_token_cached_second_call_no_reauth(self):
        adapter = _make_adapter()
        with respx.mock:
            auth_route = respx.post(_TOKEN_URL).mock(return_value=_token_response())
            respx.get(_SEARCH_URL).mock(return_value=_search_response([_SAMPLE_PRODUCT]))
            await adapter.search("coffee", SearchFilters())
            await adapter.search("filters", SearchFilters())
            # Auth endpoint called exactly once — token was cached
            assert auth_route.called
            assert len(auth_route.calls) == 1

    @pytest.mark.asyncio
    async def test_missing_credentials_raises_adapter_error(self):
        adapter = ShopifyCatalogAdapter(slug="shopify", config={})
        with pytest.raises(AdapterError, match="client_id"):
            await adapter.search("coffee", SearchFilters())

    @pytest.mark.asyncio
    async def test_auth_http_error_raises_adapter_error(self):
        adapter = _make_adapter()
        with respx.mock:
            respx.post(_TOKEN_URL).mock(return_value=httpx.Response(403))
            with pytest.raises(AdapterError, match="auth failed"):
                await adapter.search("coffee", SearchFilters())


class TestSearch:
    @pytest.mark.asyncio
    async def test_returns_normalized_products(self):
        adapter = _make_adapter()
        with respx.mock:
            respx.post(_TOKEN_URL).mock(return_value=_token_response())
            respx.get(_SEARCH_URL).mock(return_value=_search_response([_SAMPLE_PRODUCT]))
            results = await adapter.search("coffee", SearchFilters())

        assert len(results) == 1
        p = results[0]
        assert p.sku == "shopify:abc123"
        assert p.title == "Arabica Coffee Filters 100-pack"
        assert p.price == 12.99
        assert p.availability == "InStock"

    @pytest.mark.asyncio
    async def test_out_of_stock_variant(self):
        adapter = _make_adapter()
        product = {**_SAMPLE_PRODUCT, "variants": [{"price": "5.00", "available": False}]}
        with respx.mock:
            respx.post(_TOKEN_URL).mock(return_value=_token_response())
            respx.get(_SEARCH_URL).mock(return_value=_search_response([product]))
            results = await adapter.search("widget", SearchFilters())

        assert results[0].availability == "OutOfStock"

    @pytest.mark.asyncio
    async def test_sku_namespaced_with_slug(self):
        adapter = _make_adapter()
        with respx.mock:
            respx.post(_TOKEN_URL).mock(return_value=_token_response())
            respx.get(_SEARCH_URL).mock(return_value=_search_response([_SAMPLE_PRODUCT]))
            results = await adapter.search("coffee", SearchFilters())

        assert results[0].sku.startswith("shopify:")

    @pytest.mark.asyncio
    async def test_empty_results_returns_empty_list(self):
        adapter = _make_adapter()
        with respx.mock:
            respx.post(_TOKEN_URL).mock(return_value=_token_response())
            respx.get(_SEARCH_URL).mock(return_value=_search_response([]))
            results = await adapter.search("zzznomatch", SearchFilters())

        assert results == []

    @pytest.mark.asyncio
    async def test_max_price_filter_applied(self):
        adapter = _make_adapter()
        products = [
            {**_SAMPLE_PRODUCT, "upid": "cheap", "variants": [{"price": "5.00", "available": True}]},
            {**_SAMPLE_PRODUCT, "upid": "expensive", "variants": [{"price": "50.00", "available": True}]},
        ]
        with respx.mock:
            respx.post(_TOKEN_URL).mock(return_value=_token_response())
            respx.get(_SEARCH_URL).mock(return_value=_search_response(products))
            results = await adapter.search("item", SearchFilters(max_price=10.0))

        assert len(results) == 1
        assert results[0].sku == "shopify:cheap"

    @pytest.mark.asyncio
    async def test_in_stock_only_filter(self):
        adapter = _make_adapter()
        products = [
            {**_SAMPLE_PRODUCT, "upid": "instock", "variants": [{"price": "5.00", "available": True}]},
            {**_SAMPLE_PRODUCT, "upid": "oos", "variants": [{"price": "5.00", "available": False}]},
        ]
        with respx.mock:
            respx.post(_TOKEN_URL).mock(return_value=_token_response())
            respx.get(_SEARCH_URL).mock(return_value=_search_response(products))
            results = await adapter.search("item", SearchFilters(in_stock_only=True))

        assert all(r.availability == "InStock" for r in results)


class TestUnsupportedOperations:
    @pytest.mark.asyncio
    async def test_get_product_raises(self):
        adapter = _make_adapter()
        with pytest.raises(ProductNotFoundError):
            await adapter.get_product("shopify:abc123")

    @pytest.mark.asyncio
    async def test_create_order_raises(self):
        adapter = _make_adapter()
        with pytest.raises(CheckoutNotSupportedError):
            await adapter.create_order("shopify:abc123", 1, "m-123", "idem-key")

    @pytest.mark.asyncio
    async def test_get_capabilities_returns_search_true(self):
        adapter = _make_adapter()
        caps = await adapter.get_capabilities()
        assert caps["search"] is True
        assert caps["order_create"] is False
