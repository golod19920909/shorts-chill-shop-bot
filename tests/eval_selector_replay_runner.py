"""Blind replay of reviewed real AliExpress selector snapshots against Gemini models.

This runner is intentionally separate from the conversation-turn benchmark.
It requires raw captures to be independently reviewed and marked
capture_kind=real_api_response with non-null gold labels before any API call.
It never searches AliExpress: both models receive the same frozen candidate pool.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import inspect
import random
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import services.ai_service as ai_service_module
from models.product import Availability, Marketplace, Product
from services.ai_service import AIService
from services.gemini_pricing import estimate_cost_usd
from services.gemini_telemetry import extract_usage

CANDIDATE_FIELDS = (
    "id", "title", "description", "category_path", "price", "currency",
    "discount_percent", "rating", "positive_rate", "orders_count",
    "has_image", "availability",
)
QUALITY_SCORE_WEIGHTS = {
    "query_match": 30,
    "product_type": 20,
    "required_features": 15,
    "compatibility": 15,
    "practical_appeal": 10,
}
REJECTION_REASONS = {
    "wrong_product_type",
    "wrong_product_identity",
    "accessory_or_part",
    "mandatory_feature_missing",
    "compatibility_mismatch",
    "budget_violation",
    "explicit_exclusion",
    "unsupported_evidence",
    "duplicate_or_near_duplicate",
    "low_relevance",
    "other",
}


def _sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_snapshot(snapshot: object, path: Path) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise ValueError(f"{path.name}: snapshot must be a JSON object")
    source = snapshot.get("source")
    request = snapshot.get("request")
    gold = snapshot.get("gold")
    candidates = snapshot.get("candidates")
    if not isinstance(source, dict) or not isinstance(request, dict):
        raise ValueError(f"{path.name}: source/request object is missing")
    if source.get("provider") != "AliExpress Affiliate API":
        raise ValueError(f"{path.name}: unexpected provider")
    if source.get("capture_kind") != "real_api_response":
        raise ValueError(f"{path.name}: capture provenance is not verified")
    if not isinstance(source.get("page_count"), int) or source["page_count"] < 1:
        raise ValueError(f"{path.name}: page_count must be independently verified")
    if not isinstance(gold, dict):
        raise ValueError(f"{path.name}: independent gold labels are required")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError(f"{path.name}: candidate pool is empty")
    if source.get("candidate_count") != len(candidates):
        raise ValueError(f"{path.name}: candidate_count mismatch")
    if not isinstance(request.get("query"), str) or not request["query"].strip():
        raise ValueError(f"{path.name}: exact selector query is required")
    wanted = request.get("wanted")
    if not isinstance(wanted, int) or isinstance(wanted, bool) or not 1 <= wanted <= 20:
        raise ValueError(f"{path.name}: wanted must be in [1, 20]")
    for field in ("required_features", "identity_review_ids", "marketplace_terms"):
        values = request.get(field)
        if not isinstance(values, list) or not all(isinstance(x, str) for x in values):
            raise ValueError(f"{path.name}: request.{field} must be a string list")
    ids = []
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise ValueError(f"{path.name}: candidate {index} is not an object")
        if not isinstance(candidate.get("id"), str) or not candidate["id"].strip():
            raise ValueError(f"{path.name}: candidate {index} has no id")
        if not isinstance(candidate.get("title"), str) or not candidate["title"].strip():
            raise ValueError(f"{path.name}: candidate {index} has no title")
        if set(candidate) != set(CANDIDATE_FIELDS):
            raise ValueError(f"{path.name}: candidate {index} fields do not match selector payload")
        ids.append(candidate["id"])
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path.name}: duplicate candidate IDs")

    candidate_ids = set(ids)
    if not set(request["identity_review_ids"]) <= candidate_ids:
        raise ValueError(f"{path.name}: identity_review_ids contains an unknown candidate ID")
    eligible = gold.get("eligible_ids")
    rejected = gold.get("rejected")
    evidence = gold.get("required_evidence")
    identity_evidence = gold.get("identity_evidence")
    tiers = gold.get("ranking_tiers")
    quality_scores = gold.get("quality_scores")
    if not isinstance(eligible, list) or not all(isinstance(x, str) for x in eligible):
        raise ValueError(f"{path.name}: gold.eligible_ids must be a string list")
    if len(eligible) != len(set(eligible)) or not set(eligible) <= candidate_ids:
        raise ValueError(f"{path.name}: invalid eligible IDs")
    if (
        not isinstance(rejected, list)
        or not isinstance(evidence, dict)
        or not isinstance(identity_evidence, dict)
        or not isinstance(tiers, list)
        or not isinstance(quality_scores, dict)
    ):
        raise ValueError(
            f"{path.name}: rejected/evidence/identity_evidence/ranking_tiers/quality_scores labels are required"
        )
    rejected_ids = []
    rejected_by_id = {}
    for row in rejected:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("id"), str)
            or row.get("reason") not in REJECTION_REASONS
            or not isinstance(row.get("critical"), bool)
        ):
            raise ValueError(f"{path.name}: rejected labels require id, reason and critical boolean")
        rejected_ids.append(row["id"])
        rejected_by_id[row["id"]] = row
    if len(rejected_ids) != len(set(rejected_ids)) or not set(rejected_ids) <= candidate_ids:
        raise ValueError(f"{path.name}: invalid rejected IDs")
    if set(eligible) & set(rejected_ids):
        raise ValueError(f"{path.name}: eligible and rejected IDs overlap")
    if set(eligible) | set(rejected_ids) != candidate_ids:
        raise ValueError(f"{path.name}: every candidate must be independently labeled eligible or rejected")
    if not set(evidence) <= set(eligible):
        raise ValueError(f"{path.name}: required_evidence contains a non-eligible ID")
    if not set(identity_evidence) <= set(eligible):
        raise ValueError(f"{path.name}: identity_evidence contains a non-eligible ID")
    if not set(identity_evidence) <= set(request["identity_review_ids"]):
        raise ValueError(f"{path.name}: identity_evidence contains an unreviewed ID")
    required_identity = set(request["identity_review_ids"]) & set(eligible)
    if not required_identity <= set(identity_evidence):
        raise ValueError(f"{path.name}: identity evidence is incomplete")
    required_features = set(request["required_features"])
    for product_id in eligible:
        if not required_features <= set(evidence.get(product_id, {})):
            raise ValueError(f"{path.name}: required evidence is incomplete for {product_id}")

    candidate_by_id = {row["id"]: row for row in candidates}

    def quote_is_grounded(product_id: str, quote: object) -> bool:
        if not isinstance(quote, str) or not quote.strip():
            return False
        candidate = candidate_by_id[product_id]
        source = " ".join(
            f"{candidate['title']} {candidate.get('description', '')}".split()
        )
        normalized_quote = " ".join(quote.split())
        return normalized_quote in source

    for product_id, quote in identity_evidence.items():
        if not quote_is_grounded(product_id, quote):
            raise ValueError(f"{path.name}: identity evidence quote is not present in listing {product_id}")
    for product_id, evidence_map in evidence.items():
        if not isinstance(evidence_map, dict):
            raise ValueError(f"{path.name}: invalid evidence for {product_id}")
        if not set(evidence_map) <= required_features:
            raise ValueError(f"{path.name}: evidence contains an unrequested feature for {product_id}")
        for requirement, quote in evidence_map.items():
            if not quote_is_grounded(product_id, quote):
                raise ValueError(f"{path.name}: required evidence quote is not present in listing {product_id}")

    if set(quality_scores) != set(eligible):
        raise ValueError(f"{path.name}: quality_scores must cover every eligible ID exactly")
    score_fields = set(QUALITY_SCORE_WEIGHTS)
    for product_id, score in quality_scores.items():
        if not isinstance(score, dict) or set(score) != score_fields | {"diversity_group"}:
            raise ValueError(f"{path.name}: invalid quality score fields for {product_id}")
        for field in score_fields:
            value = score[field]
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 5:
                raise ValueError(f"{path.name}: {field} score for {product_id} must be an integer in [0, 5]")
        if not isinstance(score["diversity_group"], str) or not score["diversity_group"].strip():
            raise ValueError(f"{path.name}: diversity_group is required for {product_id}")

    tier_ids = [product_id for tier in tiers for product_id in tier] if all(
        isinstance(tier, list) and all(isinstance(x, str) for x in tier)
        for tier in tiers
    ) else []
    if any(not isinstance(tier, list) or not all(isinstance(x, str) for x in tier) for tier in tiers):
        raise ValueError(f"{path.name}: ranking tiers must be lists of IDs")
    if len(tier_ids) != len(set(tier_ids)) or set(tier_ids) != set(eligible):
        raise ValueError(f"{path.name}: ranking tiers must cover every eligible ID exactly once")
    return snapshot


def load_snapshots(directory: Path, *, max_cases: int = 0) -> list[dict[str, Any]]:
    files = sorted(directory.glob("*.json"))
    snapshots = []
    for path in files:
        raw = json.loads(path.read_text(encoding="utf-8"))
        snapshots.append(validate_snapshot(raw, path))
        snapshots[-1]["_source_file"] = path.name
    if max_cases > 0:
        snapshots = snapshots[:max_cases]
    if not snapshots:
        raise ValueError(f"No reviewed selector snapshots found in {directory}")
    return snapshots


def products_from_snapshot(snapshot: dict[str, Any]) -> list[Product]:
    products: list[Product] = []
    for candidate in snapshot["candidates"]:
        product_id = candidate["id"]
        product = Product.model_validate({
            "marketplace": Marketplace.ALIEXPRESS,
            "product_id": product_id,
            "title": candidate["title"],
            # These placeholders are never sent to Gemini; _rank_payload
            # deliberately excludes product_url and image_url, retaining only
            # the exact has_image boolean from the frozen selector input.
            "product_url": f"https://example.invalid/product/{product_id}",
            "description": candidate["description"],
            "category_path": candidate["category_path"],
            "image_url": (
                f"https://example.invalid/image/{product_id}"
                if candidate["has_image"] else None
            ),
            "price": candidate["price"],
            "currency": candidate["currency"],
            "discount_percent": candidate["discount_percent"],
            "rating": candidate["rating"],
            "positive_rate": candidate["positive_rate"],
            "orders_count": candidate["orders_count"],
            "availability": candidate["availability"] or Availability.UNKNOWN.value,
        })
        products.append(product)

    replay_payload = [AIService._rank_payload(product) for product in products]
    expected_payload = [
        {field: candidate[field] for field in CANDIDATE_FIELDS}
        for candidate in snapshot["candidates"]
    ]
    if replay_payload != expected_payload:
        raise ValueError(
            f"{snapshot.get('case_id', 'unknown')}: reconstructed selector payload differs"
        )
    return products


def score_selection(
    selected: list[str] | None,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    gold = snapshot["gold"]
    eligible = set(gold["eligible_ids"])
    candidate_ids = {row["id"] for row in snapshot["candidates"]}
    selected_ids = list(selected or [])
    selected_set = set(selected_ids)
    true_positive = len(selected_set & eligible)
    false_positive = len(selected_set - eligible)
    false_negative = len(eligible - selected_set)
    precision = true_positive / len(selected_set) if selected_set else (1.0 if not eligible else 0.0)
    recall = true_positive / len(eligible) if eligible else (1.0 if not selected_set else 0.0)
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall else 0.0
    )

    tier_by_id = {
        product_id: tier_index
        for tier_index, tier in enumerate(gold["ranking_tiers"])
        for product_id in tier
    }
    ranked_pairs = []
    for left_index, left in enumerate(selected_ids):
        for right in selected_ids[left_index + 1:]:
            if left in tier_by_id and right in tier_by_id:
                left_tier, right_tier = tier_by_id[left], tier_by_id[right]
                if left_tier != right_tier:
                    ranked_pairs.append(left_tier < right_tier)
    ranking_pair_accuracy = (
        sum(ranked_pairs) / len(ranked_pairs) if ranked_pairs else None
    )

    quality_scores = gold["quality_scores"]

    def item_quality_score(product_id: str) -> float:
        row = quality_scores[product_id]
        return sum(
            row[field] * weight
            for field, weight in QUALITY_SCORE_WEIGHTS.items()
        ) / 5.0

    selected_item_scores = [
        item_quality_score(product_id) if product_id in eligible else 0.0
        for product_id in selected_ids
    ]
    correct_abstention = not eligible and not selected_set
    base_quality_score = (
        sum(selected_item_scores) / len(selected_item_scores)
        if selected_item_scores
        else (90.0 if correct_abstention else 0.0)
    )
    eligible_groups = {
        quality_scores[product_id]["diversity_group"]
        for product_id in eligible
    }
    selected_groups = {
        quality_scores[product_id]["diversity_group"]
        for product_id in selected_set & eligible
    }
    diversity_score = (
        10.0 * len(selected_groups) / len(eligible_groups)
        if eligible_groups
        else (10.0 if correct_abstention else 0.0)
    )
    weighted_quality_score = base_quality_score + diversity_score
    eligible_quality_total = sum(item_quality_score(product_id) for product_id in eligible)
    selected_quality_total = sum(
        item_quality_score(product_id) for product_id in selected_set & eligible
    )
    weighted_quality_coverage = (
        selected_quality_total / eligible_quality_total * 100.0
        if eligible_quality_total
        else (100.0 if correct_abstention else 0.0)
    )
    rejected_by_id = {row["id"]: row for row in gold["rejected"]}
    critical_false_positive_ids = sorted(
        product_id
        for product_id in selected_set
        if product_id in rejected_by_id and rejected_by_id[product_id]["critical"]
    )
    critical_error_reasons: dict[str, int] = {}
    for product_id in critical_false_positive_ids:
        reason = rejected_by_id[product_id]["reason"]
        critical_error_reasons[reason] = critical_error_reasons.get(reason, 0) + 1

    required_features = set(snapshot["request"]["required_features"])
    missing_required_evidence = {
        product_id: sorted(required_features - set(gold["required_evidence"].get(product_id, {})))
        for product_id in selected_set & eligible
        if required_features - set(gold["required_evidence"].get(product_id, {}))
    }
    identity_review_ids = set(snapshot["request"]["identity_review_ids"])
    missing_identity_evidence = sorted(
        product_id
        for product_id in selected_set & identity_review_ids & eligible
        if product_id not in gold["identity_evidence"]
    )
    return {
        "selected_count": len(selected_ids),
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
        "unknown_ids": sorted(selected_set - candidate_ids),
        "duplicate_ids": len(selected_ids) - len(selected_set),
        "over_limit": len(selected_ids) > snapshot["request"]["wanted"],
        "correct_abstention": correct_abstention,
        "missing_required_evidence": missing_required_evidence,
        "missing_identity_evidence": missing_identity_evidence,
        "gold_evidence_complete": not missing_required_evidence and not missing_identity_evidence,
        "ranking_pair_accuracy": (
            round(ranking_pair_accuracy, 6)
            if ranking_pair_accuracy is not None else None
        ),
        "weighted_quality_score": round(weighted_quality_score, 6),
        "weighted_quality_coverage": round(weighted_quality_coverage, 6),
        "diversity_score": round(diversity_score, 6),
        "critical_false_positive_count": len(critical_false_positive_ids),
        "critical_false_positive_ids": critical_false_positive_ids,
        "critical_error_reasons": critical_error_reasons,
    }


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) * fraction + 0.999999)) - 1))
    return round(ordered[index], 1)


def _build_services(
    api_key: str,
    models: dict[str, str],
    thinking_levels: dict[str, str],
    timeout_seconds: float,
):
    services = {}
    for alias, model in models.items():
        services[alias] = AIService(
            api_key=api_key,
            model=model,
            fast_model=model,
            fallback_model=model,  # same ID: no fallback/retry masks this model's result
            timeout_seconds=timeout_seconds,
            conversation_selection_thinking_level=thinking_levels[alias],
        )
    return services


async def run_replay(
    snapshots: list[dict[str, Any]],
    *,
    api_key: str,
    models: dict[str, str],
    thinking_levels: dict[str, str],
    repeat: int,
    timeout_seconds: float,
    seed: int,
    output_path: Path,
    map_path: Path,
) -> None:
    rng = random.Random(seed)
    aliases = list(models)
    alias_order = aliases[:]
    rng.shuffle(alias_order)
    alias_map = {
        "seed": seed,
        "models": {
            alias: {
                "model": models[alias],
                "provider": "google",
                "thinking_level": thinking_levels[alias],
            }
            for alias in alias_order
        },
        "snapshot_hashes": {
            snapshot["case_id"]: _sha256(snapshot["candidates"])
            for snapshot in snapshots
        },
    }

    services = _build_services(api_key, models, thinking_levels, timeout_seconds)
    rows: list[dict[str, Any]] = []
    performance_rows: list[dict[str, Any]] = []
    active_attempts: list[dict[str, Any]] = []
    original_record_attempt = ai_service_module.record_attempt

    def capture_attempt(
        *, operation: str, model: str, duration_ms: float, outcome: str,
        response: object = None, error: bool = False,
    ):
        tokens = extract_usage(response) if response is not None else None
        cost, cost_status, _pricing_version = estimate_cost_usd(model, tokens)
        active_attempts.append({
            "duration_ms": round(float(duration_ms), 1),
            "outcome": outcome,
            "tokens": tokens,
            "cost_usd": cost,
            "cost_status": cost_status,
        })

    # Avoid writing evaluation calls into the bot's persistent production
    # telemetry database. All usage is captured in this local result bundle.
    ai_service_module.record_attempt = capture_attempt
    tasks = [
        (snapshot, alias, repetition)
        for snapshot in snapshots
        for repetition in range(1, repeat + 1)
        for alias in alias_order
    ]
    rng.shuffle(tasks)
    try:
        for snapshot, alias, repetition in tasks:
            active_attempts.clear()
            request = snapshot["request"]
            products = products_from_snapshot(snapshot)
            started = time.perf_counter()
            status = "accepted"
            selected = None
            error_class = None
            try:
                selected = await services[alias].select_conversation_candidates(
                    products,
                    query=request["query"],
                    wanted=request["wanted"],
                    required_features=request["required_features"],
                    identity_review_ids=request["identity_review_ids"],
                    marketplace_terms=request["marketplace_terms"],
                    budget_constraint=request.get("budget_constraint"),
                )
                if selected is None:
                    status = "unavailable"
            except Exception as exc:  # noqa: BLE001 - one bad case never ends replay
                status = "error"
                error_class = type(exc).__name__
            duration_ms = (time.perf_counter() - started) * 1000
            score = score_selection(selected, snapshot) if status == "accepted" else None
            attempt_tokens = [
                attempt["tokens"] for attempt in active_attempts if attempt["tokens"]
            ]
            tokens = None
            if attempt_tokens:
                token_fields = (
                    "prompt_token_count", "candidates_token_count",
                    "thoughts_token_count", "cached_content_token_count",
                    "total_token_count",
                )
                tokens = {
                    key: sum(item.get(key, 0) or 0 for item in attempt_tokens)
                    for key in token_fields
                }
            costs = [attempt["cost_usd"] for attempt in active_attempts if attempt["cost_usd"] is not None]
            cost = round(sum(costs), 12) if costs else None
            row = {
                "case_id": snapshot["case_id"],
                "repetition": repetition,
                "alias": alias,
                "status": status,
                "candidate_count": len(products),
                "selected_ids": selected or [],
                "duration_ms": round(duration_ms, 1),
                "attempts": len(active_attempts),
                "attempt_outcomes": [attempt["outcome"] for attempt in active_attempts],
                "tokens": tokens,
                "cost_usd": cost,
                "cost_status": "estimated" if cost is not None else "unknown_usage",
                "score": score,
                "error_class": error_class,
            }
            rows.append(row)
            performance_rows.append({
                "alias": alias,
                "duration_ms": duration_ms,
                "status": status,
                "tokens": tokens,
                "cost_usd": cost,
                "score": score,
                "attempt_outcomes": [attempt["outcome"] for attempt in active_attempts],
            })
    finally:
        ai_service_module.record_attempt = original_record_attempt
        for service in services.values():
            client = service._client
            aio = getattr(client, "aio", None) if client is not None else None
            close = getattr(aio, "aclose", None)
            if callable(close):
                await close()

    summaries = {}
    private_performance = {}
    for alias in aliases:
        group = [row for row in performance_rows if row["alias"] == alias]
        scored = [row["score"] for row in group if row["score"] is not None]
        total_selected = sum(score["selected_count"] for score in scored)
        total_tp = sum(score["true_positive"] for score in scored)
        total_fp = sum(score["false_positive"] for score in scored)
        total_fn = sum(score["false_negative"] for score in scored)
        precision = total_tp / (total_tp + total_fp) if total_tp + total_fp else 1.0
        recall = total_tp / (total_tp + total_fn) if total_tp + total_fn else 1.0
        durations = [row["duration_ms"] for row in group]
        weighted_scores = [score["weighted_quality_score"] for score in scored]
        weighted_coverages = [score["weighted_quality_coverage"] for score in scored]
        costs = [row["cost_usd"] for row in group if row["cost_usd"] is not None]
        accepted_costs = [
            row["cost_usd"] for row in group
            if row["status"] == "accepted" and row["cost_usd"] is not None
        ]
        nonempty_valid_costs = [
            row["cost_usd"] for row in group
            if (
                row["status"] == "accepted"
                and row["cost_usd"] is not None
                and row["score"] is not None
                and row["score"]["selected_count"] > 0
                and row["score"]["critical_false_positive_count"] == 0
                and row["score"]["gold_evidence_complete"]
            )
        ]
        summaries[alias] = {
            "requests": len(group),
            "accepted": sum(row["status"] == "accepted" for row in group),
            "unavailable_or_error": sum(row["status"] != "accepted" for row in group),
            "timeouts": sum(
                outcome == "timeout"
                for row in group for outcome in row["attempt_outcomes"]
            ),
            "invalid_shapes": sum(
                outcome == "invalid_shape"
                for row in group for outcome in row["attempt_outcomes"]
            ),
            "empty_responses": sum(
                outcome == "empty"
                for row in group for outcome in row["attempt_outcomes"]
            ),
            "precision_micro": round(precision, 6),
            "recall_micro": round(recall, 6),
            "f1_micro": round(
                2 * precision * recall / (precision + recall)
                if precision + recall else 0.0, 6
            ),
            "weighted_quality_score_mean": (
                round(sum(weighted_scores) / len(weighted_scores), 6)
                if weighted_scores else None
            ),
            "weighted_quality_coverage_mean": (
                round(sum(weighted_coverages) / len(weighted_coverages), 6)
                if weighted_coverages else None
            ),
            "critical_false_positives": sum(
                score["critical_false_positive_count"] for score in scored
            ),
            "critical_error_reasons": {
                reason: sum(score["critical_error_reasons"].get(reason, 0) for score in scored)
                for reason in sorted(REJECTION_REASONS)
                if any(score["critical_error_reasons"].get(reason, 0) for score in scored)
            },
            "false_positive": total_fp,
            "false_negative": total_fn,
            "latency_p50_ms": _percentile(durations, 0.50),
            "latency_p95_ms": _percentile(durations, 0.95),
            "cost_usd_total_known": round(sum(costs), 8) if costs else None,
            "cost_requests_covered": len(costs),
            "correct_abstentions": sum(
                bool(score["correct_abstention"]) for score in scored
            ),
            "gold_evidence_complete_cases": sum(
                bool(score["gold_evidence_complete"]) for score in scored
            ),
            "evidence_incomplete_cases": sum(
                not bool(score["gold_evidence_complete"]) for score in scored
            ),
            "over_limit_cases": sum(bool(score["over_limit"]) for score in scored),
        }
        private_performance[alias] = {
            "mean_cost_usd_per_accepted_decision": (
                round(sum(accepted_costs) / len(accepted_costs), 10)
                if accepted_costs else None
            ),
            "accepted_cost_coverage": len(accepted_costs),
            "mean_cost_usd_per_nonempty_valid_selection": (
                round(sum(nonempty_valid_costs) / len(nonempty_valid_costs), 10)
                if nonempty_valid_costs else None
            ),
            "nonempty_valid_selection_cost_coverage": len(nonempty_valid_costs),
            "all_attempt_cost_coverage": len(costs),
        }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps({
        "manifest": {
            "dataset_version": "selector-snapshot-v1",
            "snapshot_count": len(snapshots),
            "repeat": repeat,
            "timeout_seconds": timeout_seconds,
            "thinking_levels": sorted(set(thinking_levels.values())),
            "fallbacks": 0,
            "retries": 0,
            "candidate_payload_hashes": alias_map["snapshot_hashes"],
            "selector_method_sha256": _sha256(
                inspect.getsource(AIService.select_conversation_candidates)
            ),
        },
        "summary_by_alias": summaries,
        "results": rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    map_path.parent.mkdir(parents=True, exist_ok=True)
    map_path.write_text(json.dumps({
        **alias_map,
        "performance_by_alias": private_performance,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshots-dir", default="reports/selector-captures")
    parser.add_argument("--output", default="eval-selector-blind-results.json")
    parser.add_argument("--map-output", default="eval-selector-blind-map.json")
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=float, default=8.0)
    parser.add_argument("--max-cases", type=int, default=0)
    parser.add_argument("--seed", type=int, default=20261114)
    parser.add_argument(
        "--thinking-levels", default="minimal,low,medium",
        help="Comma-separated Gemini thinking levels to compare for both models",
    )
    args = parser.parse_args()
    if not 1 <= args.repeat <= 5:
        parser.error("--repeat must be between 1 and 5")
    if not 0 < args.timeout_seconds <= 30:
        parser.error("--timeout-seconds must be >0 and <=30")

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        parser.error("GEMINI_API_KEY environment variable is required")
    model_31 = os.getenv("EVAL_GEMINI_MODEL", "gemini-3.1-flash-lite")
    model_35 = os.getenv("EVAL_GEMINI_LITE_MODEL", "gemini-3.5-flash-lite")
    if model_31 == model_35:
        parser.error("EVAL_GEMINI_MODEL and EVAL_GEMINI_LITE_MODEL must be different")
    thinking_levels_requested = [
        item.strip().lower() for item in args.thinking_levels.split(",") if item.strip()
    ]
    allowed_levels = {"minimal", "low", "medium", "high"}
    if not thinking_levels_requested or any(
        level not in allowed_levels for level in thinking_levels_requested
    ):
        parser.error("--thinking-levels must contain minimal, low, medium, and/or high")
    thinking_levels_requested = list(dict.fromkeys(thinking_levels_requested))
    snapshots = load_snapshots(Path(args.snapshots_dir), max_cases=args.max_cases)
    models: dict[str, str] = {}
    thinking_levels: dict[str, str] = {}
    for model_alias, model in (("A_31", model_31), ("B_35", model_35)):
        for level in thinking_levels_requested:
            alias = f"{model_alias}_{level}"
            models[alias] = model
            thinking_levels[alias] = level
    asyncio.run(run_replay(
        snapshots,
        api_key=api_key,
        models=models,
        thinking_levels=thinking_levels,
        repeat=args.repeat,
        timeout_seconds=args.timeout_seconds,
        seed=args.seed,
        output_path=Path(args.output),
        map_path=Path(args.map_output),
    ))


if __name__ == "__main__":
    main()
