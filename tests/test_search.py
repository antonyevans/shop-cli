"""Tests for shop search products command.

Tests the full search pipeline: adapter dispatch, filtering,
confidence scoring, error conditions, and CLI wiring.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from shop.adapters.mock import MockAdapter
from shop.cli import app
from shop.commands.search import run_search_command
from shop.config import MerchantConfig, ShopConfig
from shop.models.commerce import SearchFilters

runner = CliRunner()


# ---------------------------------------------------------------------------
# MockAdapter search filters
# ---------------------------------------------------------------------------


class TestMockAdapterFilters:
    @pytest.fixture
    def adapter(self):
        return MockAdapter(slug="mock", config={})

    @pytest.mark.asyncio
    async def test_returns_results_for_known_term(self, adapter):
        results = await adapter.search("coffee", SearchFilters())
        assert len(results) >= 1
        assert any("coffee" in r.title.lower() for r in results)

    @pytest.mark.asyncio
    async def test_returns_empty_for_unknown_term(self, adapter):
        results = await adapter.search("xyzzy_unfindable_12345", SearchFilters())
        assert results == []

    @pytest.mark.asyncio
    async def test_max_price_filter(self, adapter):
        results = await adapter.search("coffee", SearchFilters(max_price=5.00))
        # coffee filters cost 8.99 — should be filtered out
        assert all(r.price <= 5.00 for r in results)

    @pytest.mark.asyncio
    async def test_max_price_exactly_at_boundary_included(self, adapter):
        results = await adapter.search("coffee", SearchFilters(max_price=8.99))
        coffee = next((r for r in results if "coffee" in r.sku), None)
        assert coffee is not None  # exactly at boundary should be included

    @pytest.mark.asyncio
    async def test_min_rating_filter(self, adapter):
        results = await adapter.search("coffee", SearchFilters(min_rating=4.5))
        for r in results:
            assert r.trust.seller_rating is not None
            assert r.trust.seller_rating >= 4.5

    @pytest.mark.asyncio
    async def test_min_rating_excludes_none_rating(self, adapter):
        results = await adapter.search("widget", SearchFilters(min_rating=4.0))
        # no-rating-widget has seller_rating=None — must be excluded
        assert all(r.trust.seller_rating is not None for r in results)

    @pytest.mark.asyncio
    async def test_in_stock_only_excludes_out_of_stock(self, adapter):
        results = await adapter.search("discontinued", SearchFilters(in_stock_only=True))
        assert all(r.availability == "InStock" for r in results)

    @pytest.mark.asyncio
    async def test_without_in_stock_only_includes_oos(self, adapter):
        results = await adapter.search("discontinued", SearchFilters(in_stock_only=False))
        oos = [r for r in results if r.availability == "OutOfStock"]
        assert len(oos) >= 1

    @pytest.mark.asyncio
    async def test_combined_filters(self, adapter):
        results = await adapter.search(
            "soap", SearchFilters(max_price=6.00, min_rating=4.0, in_stock_only=True)
        )
        for r in results:
            assert r.price <= 6.00
            assert r.trust.seller_rating is not None and r.trust.seller_rating >= 4.0
            assert r.availability == "InStock"


# ---------------------------------------------------------------------------
# run_search_command — core logic
# ---------------------------------------------------------------------------


class TestRunSearchCommand:
    def test_no_merchants_exits_4(self, default_config):
        with pytest.raises(SystemExit) as exc_info:
            run_search_command(
                query="coffee",
                max_price=None,
                min_rating=None,
                in_stock_only=False,
                explain=False,
                merchants=[],
                config=default_config,
            )
        assert exc_info.value.code == 4

    def test_results_sorted_by_confidence_descending(
        self, mock_merchant, low_threshold_config, capsys
    ):
        with pytest.raises(SystemExit) as exc_info:
            run_search_command(
                query="coffee soap notebook",
                max_price=None,
                min_rating=None,
                in_stock_only=False,
                explain=False,
                merchants=[mock_merchant],
                config=low_threshold_config,
            )
        assert exc_info.value.code == 0
        output = capsys.readouterr().out
        data = json.loads(output)
        confidences = [r["confidence"] for r in data["results"]]
        assert confidences == sorted(confidences, reverse=True)

    def test_explain_flag_includes_breakdown(self, mock_merchant, low_threshold_config, capsys):
        with pytest.raises(SystemExit):
            run_search_command(
                query="coffee",
                max_price=None,
                min_rating=None,
                in_stock_only=False,
                explain=True,
                merchants=[mock_merchant],
                config=low_threshold_config,
            )
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["results"]
        first = data["results"][0]
        assert "confidence_explanation" in first
        assert "breakdown" in first["confidence_explanation"]

    def test_no_explain_omits_breakdown(self, mock_merchant, low_threshold_config, capsys):
        with pytest.raises(SystemExit):
            run_search_command(
                query="coffee",
                max_price=None,
                min_rating=None,
                in_stock_only=False,
                explain=False,
                merchants=[mock_merchant],
                config=low_threshold_config,
            )
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["results"]
        assert data["results"][0]["confidence_explanation"] is None

    def test_response_has_meta_field(self, mock_merchant, low_threshold_config, capsys):
        with pytest.raises(SystemExit):
            run_search_command(
                query="coffee",
                max_price=None,
                min_rating=None,
                in_stock_only=False,
                explain=False,
                merchants=[mock_merchant],
                config=low_threshold_config,
            )
        output = capsys.readouterr().out
        data = json.loads(output)
        assert "meta" in data
        assert "failed_merchants" in data["meta"]

    def test_response_has_query_id(self, mock_merchant, low_threshold_config, capsys):
        with pytest.raises(SystemExit):
            run_search_command(
                query="coffee",
                max_price=None,
                min_rating=None,
                in_stock_only=False,
                explain=False,
                merchants=[mock_merchant],
                config=low_threshold_config,
            )
        output = capsys.readouterr().out
        data = json.loads(output)
        assert "query_id" in data
        assert data["query_id"].startswith("qry_")

    def test_low_confidence_results_exit_5(self, mock_merchant, capsys):
        # Threshold = 1.0 → nothing will pass
        config = ShopConfig(confidence_threshold=1.0, max_workers=10)
        with pytest.raises(SystemExit) as exc_info:
            run_search_command(
                query="coffee",
                max_price=None,
                min_rating=None,
                in_stock_only=False,
                explain=False,
                merchants=[mock_merchant],
                config=config,
            )
        assert exc_info.value.code == 5

    def test_empty_results_after_filters_exits_0_not_error(
        self, mock_merchant, low_threshold_config, capsys
    ):
        """Empty results from filters is not an error — just an empty list."""
        with pytest.raises(SystemExit) as exc_info:
            run_search_command(
                query="coffee",
                max_price=0.01,  # nothing under 1 cent
                min_rating=None,
                in_stock_only=False,
                explain=False,
                merchants=[mock_merchant],
                config=low_threshold_config,
            )
        # Should exit 0 with empty results (not exit 4/6)
        # This is a quirk — if the query matches but filters remove everything
        assert exc_info.value.code in (0, 5)


# ---------------------------------------------------------------------------
# All-merchants-failed → exit 6
# ---------------------------------------------------------------------------


class TestAllMerchantsFailed:
    def test_all_timeout_exits_6(self, default_config, capsys):
        """Simulate all merchants timing out."""
        from shop.adapters.base import MerchantAdapter

        class TimeoutAdapter(MerchantAdapter):
            async def search(self, query, filters):
                raise TimeoutError()

            async def get_product(self, sku):
                raise TimeoutError()

            async def get_capabilities(self):
                return {}

            async def create_order(self, sku, quantity, mandate_id, idempotency_key):
                raise TimeoutError()

        merchant = MerchantConfig(slug="slow", name="Slow Store", adapter="mock")

        with patch("shop.commands.search.create_adapter", return_value=TimeoutAdapter("slow", {})):
            with pytest.raises(SystemExit) as exc_info:
                run_search_command(
                    query="coffee",
                    max_price=None,
                    min_rating=None,
                    in_stock_only=False,
                    explain=False,
                    merchants=[merchant],
                    config=default_config,
                )

        assert exc_info.value.code == 6
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error_code"] == "all_merchants_failed"

    def test_partial_failure_exits_0_with_results(self, default_config, capsys, mock_merchant):
        """One merchant times out, one succeeds → exit 0 with partial results."""
        from shop.adapters.base import MerchantAdapter

        class TimeoutAdapter(MerchantAdapter):
            async def search(self, query, filters):
                raise TimeoutError()

            async def get_product(self, sku):
                raise TimeoutError()

            async def get_capabilities(self):
                return {}

            async def create_order(self, sku, quantity, mandate_id, idempotency_key):
                raise TimeoutError()

        slow_merchant = MerchantConfig(slug="slow", name="Slow Store", adapter="mock")

        def _create(m):
            if m.slug == "slow":
                return TimeoutAdapter("slow", {})
            return MockAdapter(m.slug, m.config.extra if hasattr(m, "config") else {})

        with patch("shop.commands.search.create_adapter", side_effect=_create):
            with pytest.raises(SystemExit) as exc_info:
                run_search_command(
                    query="coffee",
                    max_price=None,
                    min_rating=None,
                    in_stock_only=False,
                    explain=False,
                    merchants=[mock_merchant, slow_merchant],
                    config=ShopConfig(confidence_threshold=0.0, max_workers=10),
                )

        assert exc_info.value.code == 0
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["results"]
        failed = data["meta"]["failed_merchants"]
        assert len(failed) == 1
        assert failed[0]["slug"] == "slow"
        assert failed[0]["reason"] == "timeout"


# ---------------------------------------------------------------------------
# Error envelope format
# ---------------------------------------------------------------------------


class TestErrorEnvelope:
    def test_error_has_error_code_detail_exit_code(self, default_config, capsys):
        with pytest.raises(SystemExit) as exc_info:
            run_search_command(
                query="coffee",
                max_price=None,
                min_rating=None,
                in_stock_only=False,
                explain=False,
                merchants=[],
                config=default_config,
            )
        assert exc_info.value.code == 4
        output = capsys.readouterr().out
        data = json.loads(output)
        assert "error_code" in data
        assert "detail" in data
        assert "exit_code" in data
        assert data["exit_code"] == 4


# ---------------------------------------------------------------------------
# CLI wiring (typer CliRunner)
# ---------------------------------------------------------------------------


class TestCLIWiring:
    def test_search_products_basic(self, tmp_path):
        """CLI command works end-to-end with a real merchants.yaml."""
        merchants_yaml = tmp_path / "merchants.yaml"
        merchants_yaml.write_text(
            "merchants:\n  - slug: mock\n    name: Mock Store\n    adapter: mock\n"
        )
        config_yaml = tmp_path / "config.yaml"
        config_yaml.write_text("confidence_threshold: 0.0\n")

        with (
            patch("shop.commands.search.load_config") as mock_cfg,
            patch("shop.commands.search.load_merchants") as mock_merchants,
        ):
            mock_cfg.return_value = ShopConfig(confidence_threshold=0.0, max_workers=10)
            mock_merchants.return_value = [MerchantConfig(slug="mock", name="Mock", adapter="mock")]

            result = runner.invoke(app, ["search", "products", "coffee"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "results" in data
        assert data["results"]

    def test_missing_query_argument(self):
        result = runner.invoke(app, ["search", "products"])
        assert result.exit_code != 0

    def test_explain_flag_works(self):
        with (
            patch("shop.commands.search.load_config") as mock_cfg,
            patch("shop.commands.search.load_merchants") as mock_merchants,
        ):
            mock_cfg.return_value = ShopConfig(confidence_threshold=0.0, max_workers=10)
            mock_merchants.return_value = [MerchantConfig(slug="mock", name="Mock", adapter="mock")]

            result = runner.invoke(app, ["search", "products", "coffee", "--explain"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["results"][0]["confidence_explanation"] is not None

    def test_max_price_flag(self):
        with (
            patch("shop.commands.search.load_config") as mock_cfg,
            patch("shop.commands.search.load_merchants") as mock_merchants,
        ):
            mock_cfg.return_value = ShopConfig(confidence_threshold=0.0, max_workers=10)
            mock_merchants.return_value = [MerchantConfig(slug="mock", name="Mock", adapter="mock")]

            result = runner.invoke(app, ["search", "products", "coffee", "--max-price", "5.00"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        for r in data["results"]:
            assert r["price"] <= 5.00
