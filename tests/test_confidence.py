"""Tests for the confidence scoring rubric.

Each signal is tested at its threshold boundaries.
Final score is tested for correct weighting and clamping.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from shop.models.commerce import (
    CommerceTXTProduct,
    CommerceTXTReturns,
    CommerceTXTShipping,
    CommerceTXTTrust,
)
from shop.scoring import score


def _product(**kwargs) -> CommerceTXTProduct:
    """Build a minimal valid product, override any fields via kwargs."""
    base = dict(
        sku="mock:test",
        title="Test Product",
        price=10.00,
        availability="InStock",
        shipping=CommerceTXTShipping(cost=0.00, window_days="3-5"),
        returns=CommerceTXTReturns(window_days=30, condition="original", refund_timeline_days=5),
        trust=CommerceTXTTrust(seller_rating=4.7, review_count=100, certifications=["CPSC"]),
        price_history_30d={"min": 10.00, "max": 10.50},
        cached_at=datetime.now(UTC).isoformat(),
    )
    base.update(kwargs)
    return CommerceTXTProduct(**base)


# ---------------------------------------------------------------------------
# fields_completeness signal (weight=0.30)
# ---------------------------------------------------------------------------


class TestFieldsCompleteness:
    def test_all_required_fields_present(self):
        p = _product()
        s, _ = score(p)
        # With all fields, completeness = 1.0; contribution = 0.30
        assert s > 0.0

    def test_missing_shipping_cost_reduces_score(self):
        full = _product()
        missing = _product(shipping=CommerceTXTShipping(cost=None, window_days="3-5"))
        s_full, _ = score(full)
        s_missing, _ = score(missing)
        assert s_missing < s_full

    def test_two_missing_fields_larger_penalty(self):
        one_missing = _product(shipping=CommerceTXTShipping(cost=None, window_days="3-5"))
        two_missing = _product(shipping=CommerceTXTShipping(cost=None, window_days=None))
        s1, _ = score(one_missing)
        s2, _ = score(two_missing)
        assert s2 < s1

    def test_three_missing_fields_cumulative(self):
        three_missing = _product(
            shipping=CommerceTXTShipping(cost=None, window_days=None),
            returns=CommerceTXTReturns(window_days=None),
        )
        s, _ = score(three_missing)
        # completeness = max(0, 1 - 3*0.10) = 0.70 → contribution = 0.70 * 0.30 = 0.21
        # full product without this penalty would be higher
        full = _product()
        s_full, _ = score(full)
        assert s < s_full


# ---------------------------------------------------------------------------
# seller_rating signal (weight=0.20)
# ---------------------------------------------------------------------------


class TestSellerRating:
    def test_4_5_and_above_scores_1_0(self):
        p = _product(trust=CommerceTXTTrust(seller_rating=4.5, review_count=100, certifications=[]))
        s, exp = score(p)
        assert exp.breakdown["seller_rating"].score == 1.0

    def test_4_7_scores_1_0(self):
        p = _product(trust=CommerceTXTTrust(seller_rating=4.7, review_count=100, certifications=[]))
        _, exp = score(p)
        assert exp.breakdown["seller_rating"].score == 1.0

    def test_4_0_scores_0_7(self):
        p = _product(trust=CommerceTXTTrust(seller_rating=4.0, review_count=100, certifications=[]))
        _, exp = score(p)
        assert exp.breakdown["seller_rating"].score == 0.7

    def test_4_4_scores_0_7(self):
        p = _product(trust=CommerceTXTTrust(seller_rating=4.4, review_count=100, certifications=[]))
        _, exp = score(p)
        assert exp.breakdown["seller_rating"].score == 0.7

    def test_3_5_scores_0_4(self):
        p = _product(trust=CommerceTXTTrust(seller_rating=3.5, review_count=100, certifications=[]))
        _, exp = score(p)
        assert exp.breakdown["seller_rating"].score == 0.4

    def test_3_9_scores_0_4(self):
        p = _product(trust=CommerceTXTTrust(seller_rating=3.9, review_count=100, certifications=[]))
        _, exp = score(p)
        assert exp.breakdown["seller_rating"].score == 0.4

    def test_3_4_scores_0_0(self):
        p = _product(trust=CommerceTXTTrust(seller_rating=3.4, review_count=100, certifications=[]))
        _, exp = score(p)
        assert exp.breakdown["seller_rating"].score == 0.0

    def test_none_scores_0_0(self):
        p = _product(trust=CommerceTXTTrust(seller_rating=None, review_count=100, certifications=[]))
        _, exp = score(p)
        assert exp.breakdown["seller_rating"].score == 0.0


# ---------------------------------------------------------------------------
# review_count signal (weight=0.20)
# ---------------------------------------------------------------------------


class TestReviewCount:
    def test_50_or_more_scores_1_0(self):
        for count in [50, 100, 1000]:
            p = _product(trust=CommerceTXTTrust(seller_rating=4.5, review_count=count, certifications=[]))
            _, exp = score(p)
            assert exp.breakdown["review_count"].score == 1.0, f"count={count}"

    def test_49_scores_0_7(self):
        p = _product(trust=CommerceTXTTrust(seller_rating=4.5, review_count=49, certifications=[]))
        _, exp = score(p)
        assert exp.breakdown["review_count"].score == 0.7

    def test_10_scores_0_7(self):
        p = _product(trust=CommerceTXTTrust(seller_rating=4.5, review_count=10, certifications=[]))
        _, exp = score(p)
        assert exp.breakdown["review_count"].score == 0.7

    def test_9_scores_0_4(self):
        p = _product(trust=CommerceTXTTrust(seller_rating=4.5, review_count=9, certifications=[]))
        _, exp = score(p)
        assert exp.breakdown["review_count"].score == 0.4

    def test_1_scores_0_4(self):
        p = _product(trust=CommerceTXTTrust(seller_rating=4.5, review_count=1, certifications=[]))
        _, exp = score(p)
        assert exp.breakdown["review_count"].score == 0.4

    def test_0_scores_0_0(self):
        p = _product(trust=CommerceTXTTrust(seller_rating=4.5, review_count=0, certifications=[]))
        _, exp = score(p)
        assert exp.breakdown["review_count"].score == 0.0

    def test_none_scores_0_0(self):
        p = _product(trust=CommerceTXTTrust(seller_rating=4.5, review_count=None, certifications=[]))
        _, exp = score(p)
        assert exp.breakdown["review_count"].score == 0.0


# ---------------------------------------------------------------------------
# return_policy signal (weight=0.15)
# ---------------------------------------------------------------------------


class TestReturnPolicy:
    def test_all_three_fields_scores_1_0(self):
        p = _product(returns=CommerceTXTReturns(window_days=30, condition="original", refund_timeline_days=5))
        _, exp = score(p)
        assert exp.breakdown["return_policy"].score == pytest.approx(1.0)

    def test_two_of_three_scores_0_667(self):
        p = _product(returns=CommerceTXTReturns(window_days=30, condition="original", refund_timeline_days=None))
        _, exp = score(p)
        assert exp.breakdown["return_policy"].score == pytest.approx(2 / 3, abs=1e-3)

    def test_one_of_three_scores_0_333(self):
        p = _product(returns=CommerceTXTReturns(window_days=30, condition=None, refund_timeline_days=None))
        _, exp = score(p)
        assert exp.breakdown["return_policy"].score == pytest.approx(1 / 3, abs=1e-3)

    def test_none_of_three_scores_0_0(self):
        p = _product(returns=CommerceTXTReturns())
        _, exp = score(p)
        assert exp.breakdown["return_policy"].score == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# certifications signal (weight=0.10)
# ---------------------------------------------------------------------------


class TestCertifications:
    def test_one_cert_scores_1_0(self):
        p = _product(trust=CommerceTXTTrust(seller_rating=4.5, review_count=100, certifications=["CPSC"]))
        _, exp = score(p)
        assert exp.breakdown["certifications"].score == 1.0

    def test_multiple_certs_scores_1_0(self):
        p = _product(trust=CommerceTXTTrust(seller_rating=4.5, review_count=100, certifications=["CPSC", "ASTM"]))
        _, exp = score(p)
        assert exp.breakdown["certifications"].score == 1.0

    def test_empty_list_scores_0_5(self):
        p = _product(trust=CommerceTXTTrust(seller_rating=4.5, review_count=100, certifications=[]))
        _, exp = score(p)
        assert exp.breakdown["certifications"].score == 0.5

    def test_none_scores_0_5(self):
        p = _product(trust=CommerceTXTTrust(seller_rating=4.5, review_count=100, certifications=None))
        _, exp = score(p)
        assert exp.breakdown["certifications"].score == 0.5


# ---------------------------------------------------------------------------
# price_stability signal (weight=0.05)
# ---------------------------------------------------------------------------


class TestPriceStability:
    def test_stable_price_ratio_1_0_scores_1_0(self):
        p = _product(price_history_30d={"min": 10.0, "max": 10.0})
        _, exp = score(p)
        assert exp.breakdown["price_stability"].score == 1.0

    def test_ratio_1_10_scores_1_0(self):
        p = _product(price_history_30d={"min": 10.0, "max": 11.0})
        _, exp = score(p)
        assert exp.breakdown["price_stability"].score == 1.0

    def test_ratio_above_1_10_reduces_score(self):
        p_stable = _product(price_history_30d={"min": 10.0, "max": 11.0})
        p_volatile = _product(price_history_30d={"min": 10.0, "max": 13.0})
        _, exp_stable = score(p_stable)
        _, exp_volatile = score(p_volatile)
        assert exp_volatile.breakdown["price_stability"].score < exp_stable.breakdown["price_stability"].score

    def test_no_history_scores_0_5(self):
        p = _product(price_history_30d=None)
        _, exp = score(p)
        assert exp.breakdown["price_stability"].score == 0.5

    def test_ratio_1_5_scores_0_0(self):
        # Linear decay: 1.10 → 1.0, 1.50 → 0.0
        p = _product(price_history_30d={"min": 10.0, "max": 15.0})
        _, exp = score(p)
        assert exp.breakdown["price_stability"].score == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Final score properties
# ---------------------------------------------------------------------------


class TestFinalScore:
    def test_score_clamped_to_0_1(self):
        p = _product()
        s, _ = score(p)
        assert 0.0 <= s <= 1.0

    def test_explanation_score_matches_returned_score(self):
        p = _product()
        s, exp = score(p)
        assert s == exp.score

    def test_explanation_has_all_six_signals(self):
        p = _product()
        _, exp = score(p)
        assert set(exp.breakdown.keys()) == {
            "fields_completeness", "seller_rating", "review_count",
            "return_policy", "certifications", "price_stability",
        }

    def test_weights_sum_to_1(self):
        from shop.scoring import _WEIGHTS
        assert sum(_WEIGHTS.values()) == pytest.approx(1.0)

    def test_high_quality_product_above_0_80(self, full_product):
        s, _ = score(full_product)
        assert s >= 0.80, f"Expected high-quality product to score ≥ 0.80, got {s}"

    def test_minimal_product_below_0_80(self, minimal_product):
        s, _ = score(minimal_product)
        assert s < 0.80, f"Expected minimal product to score < 0.80, got {s}"
