from historical_edge import get_historical_edge


STUDY_ID = "pead_sweden_positive_earnings_reaction"


def test_valid_study_and_horizon_return_contract():
    result = get_historical_edge(STUDY_ID, 20)

    assert result["job"] == "historical_edge"
    assert result["study_id"] == STUDY_ID
    assert result["horizon_days"] == 20
    assert result["status"] == "not_available"


def test_unknown_study_is_not_supported():
    result = get_historical_edge("unknown_study", 20)

    assert result["status"] == "not_supported"
    assert "not supported" in result["explanation"]


def test_unverified_study_never_returns_fabricated_results():
    result = get_historical_edge(STUDY_ID, 20)

    assert result["status"] == "not_available"
    assert result["sample_size"] is None
    assert result["mean_return_pct"] is None
    assert result["median_return_pct"] is None
    assert result["positive_pct"] is None
    assert result["methodology"] is None
    assert result["source"] is None
