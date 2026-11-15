"""Offline validation tests for the reviewed selector replay runner."""

import copy
import json

import pytest

from tests.eval_selector_replay_runner import (
    load_snapshots,
    products_from_snapshot,
    score_selection,
    validate_snapshot,
)


def _candidate(product_id: str, *, has_image: bool = False):
    return {
        "id": product_id,
        "title": f"USB-C power bank {product_id}",
        "description": "USB-C Power Delivery 65W",
        "category_path": ["Power Banks"],
        "price": 29.99,
        "currency": "USD",
        "discount_percent": None,
        "rating": 4.8,
        "positive_rate": 98.5,
        "orders_count": 250,
        "has_image": has_image,
        "availability": "unknown",
    }


def _snapshot():
    candidates = [
        _candidate("p1", has_image=True),
        _candidate("p2"),
        _candidate("p3"),
    ]
    return {
        "dataset_version": "selector-snapshot-v1",
        "case_id": "S001",
        "captured_at_utc": "2026-11-14T00:00:00Z",
        "source": {
            "provider": "AliExpress Affiliate API",
            "capture_kind": "real_api_response",
            "query_language": "ru",
            "query_redacted": "[redacted]",
            "page_count": 3,
            "candidate_count": 3,
        },
        "request": {
            "query": "павербанк USB-C 65W",
            "wanted": 2,
            "required_features": ["USB-C Power Delivery 65W"],
            "identity_review_ids": ["p1"],
            "marketplace_terms": ["65W USB-C power bank"],
            "budget_constraint": "до 1500 грн",
        },
        "candidates": candidates,
        "gold": {
            "eligible_ids": ["p1", "p2"],
            "rejected": [{"id": "p3", "reason": "wrong_product_identity", "critical": True}],
            "required_evidence": {
                "p1": {"USB-C Power Delivery 65W": "USB-C Power Delivery 65W"},
                "p2": {"USB-C Power Delivery 65W": "USB-C Power Delivery 65W"},
            },
            "identity_evidence": {"p1": "USB-C power bank"},
            "ranking_tiers": [["p1"], ["p2"]],
            "quality_scores": {
                "p1": {
                    "query_match": 5,
                    "product_type": 5,
                    "required_features": 5,
                    "compatibility": 5,
                    "practical_appeal": 4,
                    "diversity_group": "power_bank",
                },
                "p2": {
                    "query_match": 5,
                    "product_type": 5,
                    "required_features": 5,
                    "compatibility": 5,
                    "practical_appeal": 5,
                    "diversity_group": "power_bank",
                },
            },
        },
    }


def test_reviewed_snapshot_validates_and_reconstructs_exact_selector_payload(tmp_path):
    snapshot = _snapshot()
    path = tmp_path / "selector.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

    loaded = load_snapshots(tmp_path)
    assert len(loaded) == 1
    products = products_from_snapshot(loaded[0])
    from services.ai_service import AIService

    assert [AIService._rank_payload(product) for product in products] == snapshot["candidates"]


def test_raw_or_unverified_capture_is_rejected_before_any_model_call(tmp_path):
    snapshot = _snapshot()
    snapshot["source"]["capture_kind"] = "selector_input_capture"
    path = tmp_path / "raw.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="provenance is not verified"):
        load_snapshots(tmp_path)


def test_snapshot_requires_every_candidate_to_be_labeled(tmp_path):
    snapshot = _snapshot()
    snapshot["gold"]["rejected"] = []
    path = tmp_path / "unlabeled-candidate.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="every candidate must be independently labeled"):
        load_snapshots(tmp_path)


def test_snapshot_requires_exact_listing_quotes_and_complete_quality_scores(tmp_path):
    snapshot = _snapshot()
    snapshot["gold"]["required_evidence"]["p1"]["USB-C Power Delivery 65W"] = "Not in listing"
    path = tmp_path / "ungrounded-quote.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="quote is not present in listing"):
        load_snapshots(tmp_path)

    snapshot = _snapshot()
    snapshot["gold"]["quality_scores"].pop("p2")
    path = tmp_path / "missing-quality-score.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="quality_scores must cover every eligible ID"):
        load_snapshots(tmp_path)


def test_snapshot_rejects_out_of_range_quality_scores(tmp_path):
    snapshot = _snapshot()
    snapshot["gold"]["quality_scores"]["p1"]["query_match"] = 6
    path = tmp_path / "invalid-quality-score.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="must be an integer in \\[0, 5\\]"):
        load_snapshots(tmp_path)


def test_snapshot_without_independent_gold_is_rejected(tmp_path):
    snapshot = _snapshot()
    snapshot["gold"] = None
    path = tmp_path / "unlabelled.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="independent gold labels"):
        load_snapshots(tmp_path)


def test_snapshot_requires_identity_evidence_for_reviewed_eligible_candidate(tmp_path):
    snapshot = _snapshot()
    snapshot["gold"]["identity_evidence"] = {}
    path = tmp_path / "missing-identity-evidence.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="identity evidence is incomplete"):
        load_snapshots(tmp_path)


def test_page_count_must_be_verified(tmp_path):
    snapshot = _snapshot()
    snapshot["source"]["page_count"] = None
    path = tmp_path / "unknown-pages.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="page_count"):
        load_snapshots(tmp_path)


def test_scoring_reports_precision_recall_false_positives_and_abstention():
    snapshot = _snapshot()
    score = score_selection(["p1", "p3"], snapshot)
    assert score["true_positive"] == 1
    assert score["false_positive"] == 1
    assert score["false_negative"] == 1
    assert score["precision"] == 0.5
    assert score["recall"] == 0.5
    assert score["f1"] == 0.5
    assert score["unknown_ids"] == []
    assert score["correct_abstention"] is False
    assert score["weighted_quality_score"] == 54.0
    assert score["weighted_quality_coverage"] == 49.438202
    assert score["critical_false_positive_count"] == 1
    assert score["critical_false_positive_ids"] == ["p3"]

    empty_gold = copy.deepcopy(snapshot)
    empty_gold["gold"]["eligible_ids"] = []
    empty_gold["gold"]["required_evidence"] = {}
    empty_gold["gold"]["identity_evidence"] = {}
    empty_gold["gold"]["ranking_tiers"] = []
    empty_gold["gold"]["quality_scores"] = {}
    empty_score = score_selection([], empty_gold)
    assert empty_score["correct_abstention"] is True
    assert empty_score["weighted_quality_score"] == 100.0
