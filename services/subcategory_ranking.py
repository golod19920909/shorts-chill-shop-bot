"""Lightweight BM25 + marketplace-signal ranking for curated catalog shelves.

This is a deterministic, CPU-only reranker. It never retrieves products or
calls an AI model. BM25 measures how strongly a listing title supports its
validated principal product type; Reciprocal Rank Fusion combines that order
with the existing marketplace value score from real offer data.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Sequence

from config.subcategories import Subcategory
from models.product import Product
from services.comparison_service import value_score
from services.convsearch.text import TOKEN_RE
from services.subcategory_quality import subcategory_product_type
_BM25_K1 = 1.5
_BM25_B = 0.75
_RRF_K = 60


def _tokens(text: str | None) -> list[str]:
    return [
        token.replace("ё", "е").casefold()
        for token in TOKEN_RE.findall(text or "")
        if len(token) >= 2
    ]


def _principal_title(product: Product) -> str:
    # Cross-sell fragments after common separators must not improve identity.
    return re.split(r"[,;|]", product.title or "", maxsplit=1)[0]


def _bm25(query: str, documents: Sequence[str]) -> list[float]:
    """Return standard Okapi BM25 scores for one query over a small pool."""

    query_terms = tuple(dict.fromkeys(_tokens(query)))
    docs = [_tokens(document) for document in documents]
    count = len(docs)
    if not count or not query_terms:
        return [0.0] * count

    lengths = [len(document) for document in docs]
    average_length = sum(lengths) / count or 1.0
    document_frequency: dict[str, int] = defaultdict(int)
    for document in docs:
        for term in set(document):
            document_frequency[term] += 1

    scores: list[float] = []
    for document, length in zip(docs, lengths):
        frequencies: dict[str, int] = defaultdict(int)
        for term in document:
            frequencies[term] += 1
        score = 0.0
        for term in query_terms:
            frequency = frequencies.get(term, 0)
            if not frequency:
                continue
            df = document_frequency[term]
            inverse_frequency = math.log(
                1.0 + (count - df + 0.5) / (df + 0.5)
            )
            denominator = frequency + _BM25_K1 * (
                1.0 - _BM25_B + _BM25_B * length / average_length
            )
            score += inverse_frequency * (
                frequency * (_BM25_K1 + 1.0) / denominator
            )
        scores.append(score)
    return scores


def _aliases_for_type(subcategory: Subcategory, type_key: str) -> tuple[str, ...]:
    for group in subcategory.product_type_groups:
        if any(alias.casefold() == type_key for alias in group):
            return tuple(group)
    return (type_key,)


def _offer_score(product: Product) -> float:
    """Normalize existing value_score and use AliExpress positive_rate honestly."""

    score = value_score(product)
    # AliExpress evaluate_rate is positive-feedback share, not a star rating.
    # Use it only when a real 0-5 rating is absent; never convert its units.
    if product.rating is None and product.positive_rate is not None:
        score += 30.0 * min(100.0, max(0.0, product.positive_rate)) / 100.0
    return min(100.0, max(0.0, score)) / 100.0


def rank_subcategory_products(
    products: Sequence[Product],
    subcategory: Subcategory,
) -> list[Product]:
    """Rank validated shelf candidates by fusing BM25 and marketplace offer order.

    BM25 and the marketplace offer score are fused with standard Reciprocal
    Rank Fusion (k=60), avoiding hand-tuned weights between unlike score scales.
    The global stable sort determines the first representative shown for each
    type; the existing finalizer still owns type/family caps and validation.
    """

    if len(products) < 2:
        return list(products)

    typed: list[tuple[Product, str]] = []
    for product in products:
        type_key = subcategory_product_type(product, subcategory) or "__unclassified__"
        typed.append((product, type_key))

    grouped: dict[str, list[Product]] = {}
    for product, type_key in typed:
        grouped.setdefault(type_key, []).append(product)

    scored: list[tuple[float, float, float, str, Product]] = []
    for type_key, candidates in grouped.items():
        aliases = _aliases_for_type(subcategory, type_key)
        titles = [_principal_title(product) for product in candidates]
        raw_by_alias = [_bm25(alias, titles) for alias in aliases]
        raw_scores = [
            max((scores[index] for scores in raw_by_alias), default=0.0)
            for index in range(len(candidates))
        ]
        text_order = sorted(
            range(len(candidates)),
            key=lambda index: (-raw_scores[index], candidates[index].product_id or ""),
        )
        text_rank = {index: rank for rank, index in enumerate(text_order, start=1)}
        offer_scores = [_offer_score(product) for product in candidates]
        offer_order = sorted(
            range(len(candidates)),
            key=lambda index: (-offer_scores[index], candidates[index].product_id or ""),
        )
        offer_rank = {index: rank for rank, index in enumerate(offer_order, start=1)}

        for index, product in enumerate(candidates):
            # Standard RRF: reciprocal ranks, not incomparable raw scores.
            fused_score = (
                1.0 / (_RRF_K + text_rank[index])
                + 1.0 / (_RRF_K + offer_rank[index])
            )
            scored.append((
                fused_score,
                offer_scores[index],
                raw_scores[index],
                product.product_id or "",
                product,
            ))

    scored.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
    return [item[4] for item in scored]
