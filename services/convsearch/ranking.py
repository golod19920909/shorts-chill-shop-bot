"""Candidate validation and requirement-aware ranking.

Adapted from the reference ``src/retrieval.py`` (separate candidate
generation from validation/ranking) and ``src/rerank.py`` (span coverage,
facet agreement, conflict penalties, deterministic tie-breaks) — but the
features are AliExpress-shaped: product type consistency, feature/exclusion
evidence, budget fit, and the bot's existing popularity signals
(``orders_count`` is cumulative sales, NOT ``latest_volume``; a positive
feedback share is NOT a star rating).

Deterministic: identical inputs and candidate data produce identical
rankings. No popularity weight can outrank a violated mandatory requirement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from models.product import Product
from services.relevance import relevance_tier
from services.convsearch.text import join_tokens, tokens
from services.convsearch.state import Slot

logger = logging.getLogger(__name__)

#: Ranking weights (the reference's weight table, retuned for AliExpress).
WEIGHTS = {
    "type": 1.0,        # product-type agreement — the mandatory anchor
    "feature": 0.4,     # per active requirement evidenced in the product
    "budget": 0.0,      # budget enforcement is a FILTER (engine step 2),
                        # not a score: a product inside the budget has no
                        # ranking advantage over one that merely lacks a
                        # price, and an over-budget one never reaches here.
    "preference": 0.15, # optional preference word found in the product
    "relevance": 0.2,   # the pipeline's own relevance_tier
    "popularity": 0.05, # log-scaled orders; small by design
    "conflict": -1.0,   # violates an exclusion (hard, cannot be outranked)
    "missing": 0.0,     # no evidence — never treated as proof of presence
}


@dataclass
class Validation:
    """Per-candidate validation verdict (5.3: retrieval ≠ validation)."""

    product_id: str
    type_ok: bool
    type_score: float
    violations: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)


def _product_text(product: Product) -> str:
    return join_tokens(product.title or "")


def validate_candidate(
    product: Product,
    state: object,
    product_type: str | None,
) -> Validation:
    """Validate one candidate against the active ledger.

    Product-type validation delegates to
    :func:`services.convsearch.type_validation.validate_product_type` — the
    hard, evidence-based gate (principal product vs incidental mention).
    """

    from services.convsearch.type_validation import validate_product_type

    text = _product_text(product)
    violations: list[str] = []
    evidence: list[str] = []

    type_verdict = validate_product_type(product, product_type)
    type_score = type_verdict.score if type_verdict.qualifies else 0.0
    type_ok = type_verdict.qualifies
    if not type_ok:
        violations.append("product_type")
    elif type_verdict.reasons:
        evidence.append(f"type:{type_verdict.reasons[0]}")

    for slot in state.active():
        if slot.kind == "exclusion":
            value = slot.value
            if value in text:
                violations.append(f"exclusion:{value}")
            else:
                # A negated word ABSENT from the product is satisfied.
                evidence.append(f"exclusion_absent:{value}")
        elif slot.kind == "feature":
            value = slot.value
            value_tokens = tokens(value)
            if not value_tokens:
                continue
            matched = sum(1 for token in value_tokens if token in text)
            if matched / len(value_tokens) >= 0.6:
                evidence.append(f"feature:{value}")
        elif slot.kind == "budget" and product.price is not None:
            try:
                limit = float(slot.value.split()[0])
            except ValueError:
                continue
            if product.price <= limit:
                evidence.append("budget")
            else:
                violations.append("budget")
        elif slot.kind == "preference":
            value_tokens = tokens(slot.value)
            if value_tokens and all(token in text for token in value_tokens):
                evidence.append(f"preference:{slot.value}")

    return Validation(
        product_id=product.product_id or "",
        type_ok=type_ok,
        type_score=type_score,
        violations=violations,
        evidence=evidence,
    )


def score_product(
    product: Product,
    state: object,
    validation: Validation,
) -> float:
    """Deterministic ranking score for one validated candidate."""

    score = 0.0
    score += WEIGHTS["type"] * validation.type_score
    score += WEIGHTS["feature"] * len([e for e in validation.evidence if e.startswith("feature:")])
    score += WEIGHTS["budget"] * (1 if "budget" in validation.evidence else 0)
    score += WEIGHTS["preference"] * len([e for e in validation.evidence if e.startswith("preference:")])

    try:
        tier = relevance_tier(product, None)
        score += WEIGHTS["relevance"] * (1 if tier >= 1 else 0)
    except Exception:  # noqa: BLE001 - relevance_tier may reject a request
        pass

    orders = product.orders_count or 0
    if orders > 0:
        import math
        score += WEIGHTS["popularity"] * min(1.0, math.log10(orders) / 6.0)

    if validation.violations:
        score += WEIGHTS["conflict"] * len(validation.violations)
    return score


def rank_candidates(
    products: list[Product],
    state: object,
    product_type: str | None,
) -> tuple[list[Product], list[Validation]]:
    """Rank the pool; return ordered products plus their validation verdicts.

    Mandatory requirements are enforced first: anything violating an
    exclusion or a forced type check is rejected *before* the popularity
    weight can matter, and rejected rows are dropped entirely — the engine
    reports the honest count rather than padding with unrelated products.
    Ties break by (score desc, product_id asc) — stable, deterministic.
    """

    validated: list[tuple[float, Product, Validation]] = []
    rejected: list[Validation] = []
    for product in products:
        validation = validate_candidate(product, state, product_type)
        score = score_product(product, state, validation)
        hard_rejected = any(
            violation.startswith("exclusion:") or violation == "product_type"
            for violation in validation.violations
        )
        if hard_rejected:
            rejected.append(validation)
            continue
        validated.append((score, product, validation))

    validated.sort(key=lambda item: (-item[0], item[1].product_id or ""))
    return [product for _score, product, _v in validated], [v for _s, _p, v in validated]
