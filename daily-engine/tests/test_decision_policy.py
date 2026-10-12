import math

from src.services.yao_scout.decision_policy import (
    allocate_portfolio, annotate_portfolio, prediction_entry_allowed,
)


def rows(regime="normal", industry=None):
    return [{"code": f"60000{i}", "industry": industry or f"行业{i}",
             "marketRegime": {"state": regime}} for i in range(1, 7)]


def test_weak_market_reduces_actual_allocation_and_keeps_cash():
    result = allocate_portfolio(rows("weak"))
    assert [r["plannedWeight"] for r in result["candidates"]] == [.2, .2, .1]
    assert result["policy"]["cashWeight"] == .5
    assert all(r["portfolioWeight"] == .2 for r in result["candidates"])


def test_all_controls_keep_caps_and_counterfactual_order_can_be_replayed():
    pool = annotate_portfolio(rows("normal", "同一行业"))
    assert len(pool) == 6
    assert all(r["portfolioWeightKind"] == "cap" and r["portfolioWeight"] == .2 for r in pool)
    result = allocate_portfolio(list(reversed(pool)))
    assert [r["code"] for r in result["candidates"]] == ["600006", "600005"]
    assert result["policy"]["plannedExposure"] == .4


def test_unknown_industry_is_one_conservative_bucket_and_codes_are_unique():
    pool = [{"code": r["code"]} for r in rows()]
    result = allocate_portfolio(pool+[pool[0]])
    assert len(result["candidates"]) == 4
    assert result["policy"]["plannedExposure"] == .4
    assert len({r["code"] for r in result["candidates"]}) == 4


def test_active_no_edge_or_invalid_prediction_cannot_fall_back_to_rule_selection():
    for mean, utility in ((-.01, .01), (.01, -.01), (math.nan, .1), (.1, None)):
        candidate = {"prediction": {"status": "active", "entryEligible": True,
                     "expectedNetReturn": mean, "utility": utility}}
        assert not prediction_entry_allowed(candidate)
    pool = [{**r, "prediction": {"status": "data_rejected", "entryEligible": False}} for r in rows()]
    result = allocate_portfolio(pool)
    assert result["candidates"] == []
    assert result["policy"]["cashWeight"] == 1


def test_cold_start_preserves_prospective_research_without_fabricated_edge():
    assert prediction_entry_allowed({"prediction": {"status": "rules_unvalidated", "entryEligible": None}})
    assert prediction_entry_allowed({"prediction": {"status": "active", "entryEligible": True,
        "expectedNetReturn": .01, "utility": .003}})
    assert not prediction_entry_allowed({"prediction": {"status": "unrecognized"}})
