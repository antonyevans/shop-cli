"""Confidence scoring rubric for CommerceTXT products.

Signals and weights (sum = 1.0):
  fields_completeness  0.30  — 7 required fields; each missing costs -0.10
  seller_rating        0.20  — ≥4.5=1.0, 4.0-4.4=0.7, 3.5-3.9=0.4, <3.5=0.0
  review_count         0.20  — ≥50=1.0, 10-49=0.7, 1-9=0.4, 0=0.0
  return_policy        0.15  — window + condition + refund_timeline all present=1.0
  certifications       0.10  — ≥1 cert=1.0, none=0.5
  price_stability      0.05  — max/min ratio ≤1.10=1.0, else scaled down

Final score is clamped to [0.0, 1.0].
"""

from __future__ import annotations

from shop.models.commerce import (
    CommerceTXTProduct,
    ConfidenceBreakdown,
    ConfidenceExplanation,
)

# Required fields for completeness check (7 total)
_REQUIRED_FIELDS = [
    lambda p: p.sku,
    lambda p: p.title,
    lambda p: p.price,
    lambda p: p.availability,
    lambda p: p.shipping.cost,
    lambda p: p.shipping.window_days,
    lambda p: p.returns.window_days,
]

_WEIGHTS: dict[str, float] = {
    "fields_completeness": 0.30,
    "seller_rating": 0.20,
    "review_count": 0.20,
    "return_policy": 0.15,
    "certifications": 0.10,
    "price_stability": 0.05,
}


def _score_fields_completeness(product: CommerceTXTProduct) -> float:
    missing = sum(1 for f in _REQUIRED_FIELDS if f(product) is None)
    return max(0.0, 1.0 - missing * 0.10)


def _score_seller_rating(product: CommerceTXTProduct) -> float:
    rating = product.trust.seller_rating
    if rating is None:
        return 0.0
    if rating >= 4.5:
        return 1.0
    if rating >= 4.0:
        return 0.7
    if rating >= 3.5:
        return 0.4
    return 0.0


def _score_review_count(product: CommerceTXTProduct) -> float:
    count = product.trust.review_count
    if count is None or count == 0:
        return 0.0
    if count >= 50:
        return 1.0
    if count >= 10:
        return 0.7
    return 0.4  # 1-9


def _score_return_policy(product: CommerceTXTProduct) -> float:
    r = product.returns
    has_window = r.window_days is not None
    has_condition = r.condition is not None
    has_refund_timeline = r.refund_timeline_days is not None
    present = sum([has_window, has_condition, has_refund_timeline])
    return present / 3.0


def _score_certifications(product: CommerceTXTProduct) -> float:
    certs = product.trust.certifications
    if certs is None or len(certs) == 0:
        return 0.5  # no certs is not disqualifying
    return 1.0


def _score_price_stability(product: CommerceTXTProduct) -> float:
    history = product.price_history_30d
    if history is None:
        return 0.5  # no history data — neutral
    low = history.get("min")
    high = history.get("max")
    if low is None or high is None or low <= 0:
        return 0.5
    ratio = high / low
    if ratio <= 1.10:
        return 1.0
    # Linear decay: 1.10 → 1.0, 1.50 → 0.0
    return max(0.0, 1.0 - (ratio - 1.10) / 0.40)


def score(product: CommerceTXTProduct) -> tuple[float, ConfidenceExplanation]:
    """Compute confidence score for a product.

    Returns:
        (score, explanation) where score is clamped to [0.0, 1.0].
    """
    signals = {
        "fields_completeness": _score_fields_completeness(product),
        "seller_rating": _score_seller_rating(product),
        "review_count": _score_review_count(product),
        "return_policy": _score_return_policy(product),
        "certifications": _score_certifications(product),
        "price_stability": _score_price_stability(product),
    }

    total = sum(signals[k] * _WEIGHTS[k] for k in signals)
    total = max(0.0, min(1.0, total))  # clamp to [0.0, 1.0]

    explanation = ConfidenceExplanation(
        score=round(total, 4),
        breakdown={
            k: ConfidenceBreakdown(score=round(signals[k], 4), weight=_WEIGHTS[k]) for k in signals
        },
    )
    return round(total, 4), explanation
