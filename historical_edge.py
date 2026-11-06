"""Read-only access to verified Historical Edge research results."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


_DATA_PATH = Path(__file__).resolve().parent / "data" / "historical_edge_studies.json"
_REQUIRED_RESULT_FIELDS = (
    "sample_size",
    "mean_return_pct",
    "median_return_pct",
    "positive_pct",
)


def _response(
    *,
    status: str,
    study_id: str,
    horizon_days: int,
    explanation: str,
    study: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    study = study or {}
    result = result or {}
    return {
        "status": status,
        "job": "historical_edge",
        "study_id": study_id,
        "horizon_days": horizon_days,
        "market": study.get("market"),
        "event_definition": study.get("event_definition"),
        "evaluation_period": study.get("evaluation_period"),
        "sample_size": result.get("sample_size"),
        "mean_return_pct": result.get("mean_return_pct"),
        "median_return_pct": result.get("median_return_pct"),
        "positive_pct": result.get("positive_pct"),
        "methodology": study.get("methodology"),
        "source": study.get("source"),
        "limitations": study.get("limitations", []),
        "explanation": explanation,
    }


def get_historical_edge(study_id: str, horizon_days: int) -> dict:
    """Return a verified historical study result without estimating missing data."""

    if not isinstance(study_id, str) or not study_id.strip():
        return _response(
            status="not_supported",
            study_id=str(study_id),
            horizon_days=horizon_days,
            explanation="study_id must be a non-empty string.",
        )

    if isinstance(horizon_days, bool) or not isinstance(horizon_days, int) or horizon_days <= 0:
        return _response(
            status="not_supported",
            study_id=study_id,
            horizon_days=horizon_days,
            explanation="horizon_days must be a positive integer.",
        )

    with _DATA_PATH.open("r", encoding="utf-8") as data_file:
        studies = json.load(data_file).get("studies", {})

    study = studies.get(study_id)
    if study is None:
        return _response(
            status="not_supported",
            study_id=study_id,
            horizon_days=horizon_days,
            explanation="The requested study_id is not supported.",
        )

    result = study.get("horizons", {}).get(str(horizon_days))
    if result is None:
        return _response(
            status="not_supported",
            study_id=study_id,
            horizon_days=horizon_days,
            study=study,
            explanation="The requested horizon is not supported for this study.",
        )

    verified = (
        study.get("status") == "verified"
        and all(result.get(field) is not None for field in _REQUIRED_RESULT_FIELDS)
        and study.get("methodology") is not None
        and study.get("source") is not None
    )
    if not verified:
        return _response(
            status="not_available",
            study_id=study_id,
            horizon_days=horizon_days,
            study=study,
            result=result,
            explanation=(
                "Verified results are not available. Missing values remain null; "
                "no estimates or new backtest results are returned."
            ),
        )

    return _response(
        status="ok",
        study_id=study_id,
        horizon_days=horizon_days,
        study=study,
        result=result,
        explanation="Verified historical research result.",
    )
