"""validate_envelope: the strict gate that drives the same-session retry."""

from __future__ import annotations

from toolengrams.consolidation import report_parse


def _valid_block():
    return {"metrics": {"surfaces_evaluated": 3, "quality_score": 0.66},
            "recommendations": [{"title": "x", "severity": "warn"}]}


def test_valid_envelope_has_no_problems():
    assert report_parse.validate_envelope(_valid_block()) == []


def test_metrics_only_is_valid():
    assert report_parse.validate_envelope({"metrics": {"quality_score": 1.0}}) == []


def test_empty_block_flags_missing_json():
    problems = report_parse.validate_envelope({})
    assert problems and "parseable JSON object" in problems[0]


def test_non_dict_flags_missing_json():
    assert report_parse.validate_envelope([1, 2]) != []  # type: ignore[arg-type]


def test_missing_metrics_is_flagged():
    problems = report_parse.validate_envelope({"recommendations": []})
    assert any("metrics" in p for p in problems)


def test_empty_metrics_is_flagged():
    problems = report_parse.validate_envelope({"metrics": {}})
    assert any("metrics" in p for p in problems)


def test_recommendations_not_a_list_is_flagged():
    block = {"metrics": {"quality_score": 1}, "recommendations": {"title": "x"}}
    assert any("recommendations" in p for p in report_parse.validate_envelope(block))


def test_recommendation_without_title_is_flagged():
    block = {"metrics": {"quality_score": 1},
             "recommendations": [{"severity": "warn"}]}
    assert any("title" in p for p in report_parse.validate_envelope(block))


def test_empty_recommendations_list_is_valid():
    assert report_parse.validate_envelope({"metrics": {"quality_score": 1},
                                           "recommendations": []}) == []


def test_envelope_problems_extracts_then_validates():
    # No JSON block at all → one problem about the missing object.
    assert report_parse.envelope_problems("just prose") != []
    # A clean trailing block → no problems.
    good = 'report\n```json\n{"metrics": {"quality_score": 0.5}}\n```'
    assert report_parse.envelope_problems(good) == []
