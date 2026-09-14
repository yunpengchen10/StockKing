from src.services.yao_scout.adaptive import probability_display_status, descriptive_rate_interval


def test_history_frequency_gate_requires_all_three_evidence_dimensions():
    state = {"availableTradingDays": 20, "historyPoints": 60, "normalTradingDays": 20}
    similar = {"status": "ready", "sampleCount": 30}
    assert probability_display_status(state, similar) == "research_rate_available"
    assert probability_display_status({**state, "availableTradingDays": 19}, similar) != "research_rate_available"
    assert probability_display_status({**state, "historyPoints": 59}, similar) != "research_rate_available"
    assert probability_display_status(state, {**similar, "sampleCount": 3}) != "research_rate_available"
    assert probability_display_status({"normalTradingDays": 100}, similar) != "research_rate_available"


def test_small_samples_do_not_receive_an_interval_and_all_success_is_not_certainty():
    assert descriptive_rate_interval(3, 3) is None
    low, high = descriptive_rate_interval(30, 30)
    assert 0 < low < 1 and high == 1
    low, high = descriptive_rate_interval(0, 30)
    assert low == 0 and 0 < high < 1
    low, high = descriptive_rate_interval(15, 30)
    assert low < 0.5 < high
