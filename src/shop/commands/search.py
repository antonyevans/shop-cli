"""shop search products — parallel UCP search across registered merchants."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Optional

import typer

from shop import scoring
from shop.config import MerchantConfig, ShopConfig, create_adapter, load_config, load_merchants
from shop.models.commerce import (
    ErrorResponse,
    FailedMerchant,
    SearchFilters,
    SearchMeta,
    SearchResponse,
    SearchResult,
)

_TIMEOUT_SECONDS = 3.0


def _emit(data: dict, exit_code: int = 0) -> None:
    """Print JSON to stdout and exit with given code."""
    print(json.dumps(data, ensure_ascii=False))
    sys.exit(exit_code)


def _error(error_code: str, detail: str, exit_code: int) -> None:
    resp = ErrorResponse(error_code=error_code, detail=detail, exit_code=exit_code)
    _emit(resp.model_dump(), exit_code)


async def _search_one(
    merchant: MerchantConfig,
    query: str,
    filters: SearchFilters,
    semaphore: asyncio.Semaphore,
) -> tuple[list, FailedMerchant | None]:
    """Search a single merchant, respecting the concurrency semaphore."""
    async with semaphore:
        adapter = create_adapter(merchant)
        start = time.monotonic()
        try:
            products = await asyncio.wait_for(
                adapter.search(query, filters), timeout=_TIMEOUT_SECONDS
            )
            return products, None
        except (TimeoutError, asyncio.TimeoutError):
            duration_ms = int((time.monotonic() - start) * 1000)
            return [], FailedMerchant(slug=merchant.slug, reason="timeout", duration_ms=duration_ms)
        except Exception:
            duration_ms = int((time.monotonic() - start) * 1000)
            return [], FailedMerchant(slug=merchant.slug, reason="error", duration_ms=duration_ms)


async def _run_search(
    query: str,
    filters: SearchFilters,
    merchants: list[MerchantConfig],
    config: ShopConfig,
    explain: bool,
) -> SearchResponse:
    semaphore = asyncio.Semaphore(config.max_workers)
    tasks = [_search_one(m, query, filters, semaphore) for m in merchants]
    gathered = await asyncio.gather(*tasks)

    all_products = []
    failed_merchants = []
    for products, failure in gathered:
        all_products.extend(products)
        if failure:
            failed_merchants.append(failure)

    results = []
    for product in all_products:
        confidence, explanation = scoring.score(product)
        result = SearchResult.from_product(
            product,
            confidence=confidence,
            explanation=explanation if explain else None,
        )
        results.append(result)

    # Sort by confidence descending
    results.sort(key=lambda r: r.confidence, reverse=True)

    return SearchResponse(
        results=results,
        total=len(results),
        meta=SearchMeta(
            failed_merchants=failed_merchants,
            total_queried=len(merchants),
        ),
    )


def run_search_command(
    query: str,
    max_price: Optional[float],
    min_rating: Optional[float],
    in_stock_only: bool,
    explain: bool,
    merchants: list[MerchantConfig],
    config: ShopConfig,
) -> None:
    """Core search logic — separated from CLI wiring for testability."""
    if not merchants:
        _error(
            "no_merchants_configured",
            "No merchants in ~/.shop/merchants.yaml. Run: shop merchant add <url>",
            exit_code=4,
        )

    filters = SearchFilters(
        max_price=max_price,
        min_rating=min_rating,
        in_stock_only=in_stock_only,
    )

    response = asyncio.run(_run_search(query, filters, merchants, config, explain))

    # All merchants failed → exit 6
    if not response.results and len(response.meta.failed_merchants) == len(merchants):
        _error(
            "all_merchants_failed",
            f"All {len(merchants)} merchant(s) timed out or errored.",
            exit_code=6,
        )

    # Check if any result is below confidence threshold
    # If ALL results are below threshold → exit 5 (low confidence)
    if response.results:
        threshold = config.confidence_threshold
        above_threshold = [r for r in response.results if r.confidence >= threshold]
        if not above_threshold:
            _emit(
                {
                    **response.model_dump(),
                    "exit_code": 5,
                    "detail": f"All results below confidence threshold ({threshold})",
                },
                exit_code=5,
            )

    _emit(response.model_dump(), exit_code=0)


app = typer.Typer()


@app.command("products")
def search_products(
    query: str = typer.Argument(..., help="Search terms"),
    max_price: Optional[float] = typer.Option(None, "--max-price", help="Maximum price in USD"),
    min_rating: Optional[float] = typer.Option(
        None, "--min-rating", help="Minimum seller rating (0-5)"
    ),
    in_stock_only: bool = typer.Option(False, "--in-stock-only", help="Only return in-stock items"),
    explain: bool = typer.Option(False, "--explain", help="Include confidence score breakdown"),
) -> None:
    """Search for products across registered merchants."""
    config = load_config()
    merchants = load_merchants()

    run_search_command(
        query=query,
        max_price=max_price,
        min_rating=min_rating,
        in_stock_only=in_stock_only,
        explain=explain,
        merchants=merchants,
        config=config,
    )
