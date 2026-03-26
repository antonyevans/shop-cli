"""CommerceTXT normalized product models and search I/O schemas.

CommerceTXT is shop's internal normalized format — NOT a merchant-published format.
Merchants implement UCP; adapters normalize UCP responses to CommerceTXT internally.
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# CommerceTXT sub-models
# ---------------------------------------------------------------------------


class CommerceTXTShipping(BaseModel):
    cost: float | None = None
    window_days: str | None = None  # e.g. "3-5"
    carrier: str | None = None


class CommerceTXTReturns(BaseModel):
    window_days: int | None = None
    restocking_fee: float | None = None
    condition: str | None = None
    refund_timeline_days: int | None = None


class CommerceTXTTrust(BaseModel):
    seller_rating: float | None = None
    review_count: int | None = None
    certifications: list[str] | None = None
    authenticity: str | None = None


class CommerceTXTProduct(BaseModel):
    """Normalized internal product representation.

    SKU is always namespaced: "{merchant_slug}:{merchant_sku}"
    All fields are always present — null if unavailable (never omitted).
    """

    sku: str  # "{merchant_slug}:{merchant_sku}"
    title: str
    description: str | None = None
    price: float
    price_history_30d: dict[str, float] | None = None  # {"min": float, "max": float}
    availability: Literal["InStock", "OutOfStock", "PreOrder", "Unknown"] = "Unknown"
    stock_count: int | None = None
    shipping: CommerceTXTShipping = Field(default_factory=CommerceTXTShipping)
    returns: CommerceTXTReturns = Field(default_factory=CommerceTXTReturns)
    trust: CommerceTXTTrust = Field(default_factory=CommerceTXTTrust)
    cached_at: str  # ISO8601
    tax_excluded: bool = True
    checkout_url: str | None = None   # Shopify per-product checkout URL
    variant_id: str | None = None     # Shopify variant GID (gid://shopify/ProductVariant/…)


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------


class ConfidenceBreakdown(BaseModel):
    score: float
    weight: float


class ConfidenceExplanation(BaseModel):
    score: float
    breakdown: dict[str, ConfidenceBreakdown]


# ---------------------------------------------------------------------------
# Search I/O
# ---------------------------------------------------------------------------


class SearchFilters(BaseModel):
    max_price: float | None = None
    min_rating: float | None = None
    in_stock_only: bool = False


class SearchResult(BaseModel):
    """A single search result — CommerceTXT product + confidence score."""

    sku: str
    title: str
    description: str | None = None
    price: float
    availability: Literal["InStock", "OutOfStock", "PreOrder", "Unknown"]
    rating: float | None = None
    review_count: int | None = None
    confidence: float
    checkout_url: str | None = None
    variant_id: str | None = None
    agent_summary: str | None = None
    confidence_explanation: ConfidenceExplanation | None = None

    @classmethod
    def from_product(
        cls,
        product: CommerceTXTProduct,
        confidence: float,
        explanation: ConfidenceExplanation | None = None,
        agent_summary: str | None = None,
    ) -> SearchResult:
        return cls(
            sku=product.sku,
            title=product.title,
            description=product.description,
            price=product.price,
            availability=product.availability,
            rating=product.trust.seller_rating,
            review_count=product.trust.review_count,
            confidence=confidence,
            checkout_url=product.checkout_url,
            variant_id=product.variant_id,
            agent_summary=agent_summary,
            confidence_explanation=explanation,
        )


class FailedMerchant(BaseModel):
    slug: str
    reason: Literal["timeout", "error"]
    duration_ms: int


class SearchMeta(BaseModel):
    failed_merchants: list[FailedMerchant] = Field(default_factory=list)
    total_queried: int = 0


class SearchResponse(BaseModel):
    results: list[SearchResult]
    total: int
    query_id: str = Field(default_factory=lambda: f"qry_{uuid.uuid4().hex[:8]}")
    meta: SearchMeta = Field(default_factory=SearchMeta)


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------


class ErrorResponse(BaseModel):
    """Returned on all non-zero exit codes."""

    error_code: str
    detail: str
    exit_code: int
