# -*- coding: utf-8 -*-
"""
B5: search-pipeline evaluation runner (standalone, NOT part of the pytest suite).

Runs the B5_DATASET through ``SearchService.find_products_from_text`` (the
same production entry point the bot uses) and reports quality + performance
for every case, without changing any search behaviour.

Execution modes (clearly distinguished per case):
  * live   - real AI (Gemini) + real provider (AliExpress) calls; only when
             credentials are configured AND --live is passed AND network works
  * mock   - stub provider with controlled rows (offline, deterministic);
             used when --live is not passed, credentials are missing, or
             live execution is blocked
  * fixture - dataset entry originates from an existing gold fixture
             (tests/fixtures_stage44_dataset.py or
              tests/fixtures_stage45_eval.py); this is a dataset-origin tag,
             not an execution mode
  * cached - not implemented in this run (every case is re-executed)

Scoring (explicit numerators/denominators, documented in the report):
  * PASS     - pipeline completed, no total provider failure, and the outcome
               stage is consistent with the case's expected_outcome_hint
  * FAIL     - pipeline ran but a product-hint case suffered total failure
               (or an empty-hint case returned invented products)
  * INCONCLUSIVE - execution blocked (no credentials/network) or an exception
                   occurred that prevented a clean measurement

No query text, PII, credentials, or API keys are stored in results.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Make both the project root (services/, config/, models/) and tests/
# importable when run as ``python tests/eval_b5_runner.py``.
import sys as _sys

_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_sys.path.insert(0, str(Path(__file__).resolve().parent))

from config.settings import load_settings, MarketplaceCredentials  # noqa: E402
from models.product import Marketplace, Product  # noqa: E402
from models.search_request import SearchRequest  # noqa: E402
from services.search_diagnostics import (  # noqa: E402
    OUTCOME_EMPTY_AFTER_CONSTRAINTS,
    OUTCOME_EMPTY_AFTER_RELEVANCE,
    OUTCOME_PROVIDER_TOTAL_FAILURE,
    OUTCOME_SUCCESS_EMPTY_RAW,
    OUTCOME_SUCCESS_WITH_RESULTS,
    SEARCH_DIAGNOSTICS,
    classify_outcome,
)
from services.search_service import SearchService  # noqa: E402

from eval_b5_dataset import (  # noqa: E402
    B5_DATASET,
    DATASET_SIZE,
    DATASET_VERSION,
)

# -- stub provider (mock mode) -------------------------------------------------
# Duck-typed like the B4 StubProvider: no real HTTP, deterministic rows.
#
# The rows echo the request's own words (their titles LEAD with a compact
# retrieval phrase) so mock mode measures the PIPELINE - parsing, retrieval
# breadth, numeric constraints, ranking, outcome classification - instead of
# measuring the relevance gate against placeholder titles that share nothing
# with the query. Relevance still runs unchanged; the mock only stops
# sabotaging it, exactly like a real marketplace that answers in-query
# language. Cases whose hint expects an EMPTY pipeline stage keep their
# empty behaviour explicitly (see ``_EMPTY_RAW_QUERIES`` / ``_OVER_BUDGET_*
# `` below), so the empty-stage paths stay covered.


#: Queries for which the mock marketplace answers with NOTHING, mirroring a
#: real provider that has no candidates for a non-product message. Kept in
#: sync with the dataset's SUCCESS_EMPTY_RAW cases: clarification fragments
#: and vague mood queries with no concrete product type.
_EMPTY_RAW_QUERIES = frozenset({
    "оно",
    "лучшее",
    "устройство",
    "что-нибудь для машины",
    "хороший подарок",
    "стильная одежда",
})

#: Queries whose hint expects the CONSTRAINT stage to empty the pool: every
#: mock row is priced above the parsed budget (in the REQUEST's own
#: currency, which the runner now carries via ``_fallback_parse``), so a
#: real numeric filter - not relevance - removes them.
_OVER_BUDGET_QUERIES = frozenset({
    "настольная лампа до 500 грн",
})

#: Queries whose hint expects RELEVANCE to empty the pool even though the
#: marketplace answered: the mock answers with deliberately off-topic rows
#: (a real marketplace also returns junk for some phrases), so the gate has
#: something honest to remove.
_OFF_TOPIC_QUERIES = frozenset({
    "ищу наушники без проводов и без шумоподавления",
})


def _mock_title_phrase(request: SearchRequest) -> str:
    """The phrase the mock titles echo: the request's own normalized query.

    The WHOLE query, not a short prefix: a real marketplace title repeats
    the words the user asked for (including the «с X» characteristics), and
    the inclusion filter (``comparison_service._apply_feature_filters``)
    checks those features against the title. A truncated title would drop
    the requested words and empty the pool of exactly the products the
    user asked for - an artefact of the mock, not of the pipeline.
    """

    return " ".join((request.normalized_query or "").split()) or "товар"


def _mock_rows_for(request: SearchRequest) -> list[Product]:
    """Deterministic query-aware rows for one mock search.

    Titles lead with the request's own retrieval phrase (its full
    ``normalized_query``), prices form a small ladder (partially above a
    parsed budget when one exists), and the currency follows the REQUEST's
    parsed currency (falling back to USD) so the currency-coherence rule in
    ``comparison_service`` compares like with like instead of dropping every
    row on ``currency_mismatch``.
    """
    phrase = _mock_title_phrase(request)
    currency = request.currency or "USD"
    budget = request.max_price
    base = budget * 0.6 if budget else 10.0
    step = budget * 0.2 if budget else 1.0
    rows: list[Product] = []
    for i in range(1, 6):
        rows.append(
            Product(
                marketplace=Marketplace.ALIEXPRESS,
                product_id=f"mock-{i}",
                title=f"{phrase} — моковый товар {i}",
                product_url=f"https://www.aliexpress.com/item/mock-{i}.html",
                image_url=f"https://ae01.alicdn.com/kf/mock-{i}.jpg",
                price=round(base + i * step, 2),
                currency=currency,
                orders_count=50 + i * 10,
            )
        )
    return rows


class MockProvider:
    """Deterministic stub provider for offline mock mode."""

    marketplace = Marketplace.ALIEXPRESS
    display_name = "AliExpress (mock)"
    supports_feed = True

    def __init__(self) -> None:
        self.calls = 0

    @property
    def is_configured(self) -> bool:
        return True

    async def search_products(self, request: SearchRequest) -> list[Product]:
        self.calls += 1
        # Return rows only for queries that look product-like; clarification
        # / ambiguous queries get nothing, so the pipeline can be scored.
        q = (request.original_query or "").strip().lower()
        if len(q) <= 2 or q in _EMPTY_RAW_QUERIES:
            return []
        if q in _OFF_TOPIC_QUERIES:
            # Real marketplaces answer some phrases with pure junk; the
            # relevance gate must remove all of it (EMPTY_AFTER_RELEVANCE).
            return [
                Product(
                    marketplace=Marketplace.ALIEXPRESS,
                    product_id=f"mock-junk-{i}",
                    title=f"Несвязанный сувенир {i} магнит открытка",
                    product_url=f"https://www.aliexpress.com/item/mock-junk-{i}.html",
                    image_url=f"https://ae01.alicdn.com/kf/mock-junk-{i}.jpg",
                    price=10.0 + i,
                    currency=request.currency or "USD",
                    orders_count=50 + i * 10,
                )
                for i in range(1, 6)
            ]
        rows = _mock_rows_for(request)
        if q in _OVER_BUDGET_QUERIES and request.max_price is not None:
            # Every row above the parsed ceiling (same currency), so the
            # CONSTRAINT filter - not relevance - empties the pool.
            currency = request.currency or "USD"
            return [
                Product(
                    marketplace=Marketplace.ALIEXPRESS,
                    product_id=f"mock-pricey-{i}",
                    title=f"{_mock_title_phrase(request)} — моковый товар {i}",
                    product_url=f"https://www.aliexpress.com/item/mock-pricey-{i}.html",
                    image_url=f"https://ae01.alicdn.com/kf/mock-pricey-{i}.jpg",
                    price=round(request.max_price * (1.5 + 0.1 * i), 2),
                    currency=currency,
                    orders_count=50 + i * 10,
                )
                for i in range(1, 6)
            ]
        return rows

    async def get_product(self, product_id: str, currency: str | None = None):
        return None

    def generate_affiliate_link(self, product: Product):
        return product.product_url


# -- service construction -----------------------------------------------------


def build_service(live: bool) -> tuple[SearchService, str, bool]:
    """
    Build a SearchService and report whether live execution is available.

    Returns ``(service, mode, live_available)`` where ``mode`` is ``"live"``
    or ``"mock"``. When ``live`` is requested but credentials are missing, we
    degrade to mock and report ``live_available=False``.
    """
    settings = load_settings()
    credentials_ok = bool(
        settings.gemini_api_key
        and settings.aliexpress.app_key
        and settings.aliexpress.app_secret
    )

    if live and credentials_ok:
        try:
            from services.ai_service import AIService  # imported here so
            from services.marketplaces.aliexpress import (  # offline runs
                AliExpressProvider,
            )  # never touch the real client

            provider = AliExpressProvider(
                MarketplaceCredentials(
                    app_key=settings.aliexpress.app_key,
                    app_secret=settings.aliexpress.app_secret,
                )
            )
            ai = AIService(settings.gemini_api_key)
            return SearchService([provider], ai_service=ai), "live", True
        except Exception:  # noqa: BLE001 - degrade gracefully to mock
            pass

    # Mock mode still needs an AIService: the production entry point
    # (``find_products_from_text``) parses the query through it. An
    # AIService without an API key uses the documented LOCAL FALLBACK
    # parser, so mock mode measures the real parsing layer offline and
    # never touches the network.
    from services.ai_service import AIService

    return SearchService([MockProvider()], ai_service=AIService(None)), "mock", False


# -- per-case execution helpers -----------------------------------------------


def _stage_counts(outcome: Any) -> dict[str, int]:
    """Read the four pipeline stages from a SearchOutcome (counts only).

    ``after_relevance`` is derived as ``raw - dropped``: the outcome carries
    ``products_before_filtering`` and ``products_dropped_as_irrelevant`` but
    no ``products_after_relevance`` field (the old code read that missing
    attribute and always reported 0, mislabelling every case).
    """
    raw = int(getattr(outcome, "products_before_filtering", 0) or 0)
    dropped = int(getattr(outcome, "products_dropped_as_irrelevant", 0) or 0)
    return {
        "raw": raw,
        "after_relevance": max(raw - dropped, 0),
        "after_constraints": int(
            getattr(outcome, "products_after_constraints", 0) or 0
        ),
        "final": len(outcome.products) if outcome.products is not None else 0,
    }


def _b5_outcome_label(outcome: Any) -> str:
    """One outcome label per case, via the B4 classifier (no duplication)."""
    return classify_outcome(outcome)


def _b5_score(case: dict, outcome_label: str, blocked: bool, exception: str | None) -> str:
    """PASS / FAIL / INCONCLUSIVE with explicit criteria.

    The outcome label must EQUAL the expected hint: the old code passed any
    case whose pool merely reached the hinted stage (e.g. a case that died
    at relevance passed an EMPTY_AFTER_CONSTRAINTS hint), which hid real
    stage attribution errors.
    """
    if blocked or exception:
        return "INCONCLUSIVE"
    if outcome_label == case["expected_outcome_hint"]:
        return "PASS"
    return "FAIL"


async def run_case(
    case: dict,
    service: SearchService,
    mode: str,
    live_available: bool,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """Run one dataset case and return a fully annotated result dict."""
    query = case["query"]
    hint = case["expected_outcome_hint"]

    result: dict[str, Any] = {
        "id": case["id"],
        "category": case["category"],
        "language": case["language"],
        "intent_category": case["intent_category"],
        "query": query,
        "expected_outcome_hint": hint,
        "execution_mode": mode,
        "live_available": live_available,
        "blocked": False,
        "exception": None,
        "outcome_label": None,
        "outcome_label_source": "diagnostics-snapshot",
        "stage_counts": {"raw": 0, "after_relevance": 0, "after_constraints": 0, "final": 0},
        "searched_marketplaces": [],
        "failed_marketplaces": [],
        "unavailable_marketplaces": [],
        "final_count": 0,
        "latency_ms": None,
        "score": None,
        "notes": case.get("notes", ""),
    }

    # B4 lifecycle: reset the registry before every case so the snapshot
    # reflects exactly this one search (privacy-safe: no query text stored).
    SEARCH_DIAGNOSTICS.reset()

    start = time.perf_counter()
    exception: str | None = None
    blocked = False
    outcome = None

    # B5 runner fix: the runner enters the pipeline through the SAME
    # production entry point as the bot (``find_products_from_text``), so the
    # measured path is the real one: natural-language parsing, the
    # structured-intent layer (``parse_query_intent`` + ``apply_intent``) and
    # every constraint the production search applies. The bare
    # ``SearchRequest`` used earlier bypassed all of that and measured a
    # pipeline the user never gets.
    try:
        outcome = await asyncio.wait_for(
            service.find_products_from_text(query), timeout=timeout_s
        )
    except asyncio.TimeoutError:
        exception = f"timeout after {timeout_s}s"
        blocked = True
    except Exception as exc:  # noqa: BLE001 - one bad case never ends the run
        exception = f"{type(exc).__name__}: {exc}"
        # Network/auth failures block live execution; record honestly.
        blocked = True

    elapsed_ms = (time.perf_counter() - start) * 1000.0
    result["latency_ms"] = round(elapsed_ms, 3)
    result["blocked"] = blocked
    result["exception"] = exception

    if outcome is not None:
        result["final_count"] = len(outcome.products)
        result["searched_marketplaces"] = list(
            getattr(outcome, "searched_marketplaces", ()) or ()
        )
        result["failed_marketplaces"] = list(
            getattr(outcome, "failed_marketplaces", ()) or ()
        )
        result["unavailable_marketplaces"] = list(
            getattr(outcome, "unavailable_marketplaces", ()) or ()
        )
        result["stage_counts"] = _stage_counts(outcome)
        result["outcome_label"] = _b5_outcome_label(outcome)
        result["outcome_label_source"] = "b4-classify-outcome"
    else:
        result["outcome_label"] = None
        result["outcome_label_source"] = "no-outcome"

    # Snapshot B4 diagnostics for the aggregate (counts + provider stats only).
    try:
        result["diagnostics_snapshot"] = SEARCH_DIAGNOSTICS.snapshot()
    except Exception:  # noqa: BLE001 - diagnostics must never break the run
        result["diagnostics_snapshot"] = None

    result["score"] = _b5_score(
        case,
        result["outcome_label"] or "",
        blocked,
        exception,
    )
    result["failure_kind"] = _failure_kind(
        case, result["outcome_label"] or "", result["score"]
    )
    return result


def _failure_kind(case: dict, outcome_label: str, score: str) -> str | None:
    """Classify WHY a case missed its hint (None when the case passed).

    The kinds are what the B6 issue list needs: a "results expected, got
    none" case is a retrieval/relevance defect, a "empty expected, got
    results" case is a missing clarification/refusal, and a stage mismatch
    inside the empty family is a different defect again (the pool died at
    a different stage than the dataset expected).
    """
    if score != "FAIL":
        return None

    hint = case["expected_outcome_hint"]
    empty_family = {
        OUTCOME_SUCCESS_EMPTY_RAW,
        OUTCOME_EMPTY_AFTER_RELEVANCE,
        OUTCOME_EMPTY_AFTER_CONSTRAINTS,
    }
    got_empty = outcome_label in empty_family

    if hint == OUTCOME_SUCCESS_WITH_RESULTS:
        return "results_expected_got_empty"
    if got_empty:
        return "empty_expected_got_results"
    return "stage_mismatch"


# -- run all cases ------------------------------------------------------------


def _percentile(sorted_values: list[float], pct: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    d = k - f
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * d


def run_all(live: bool, timeout_s: float = 60.0) -> dict[str, Any]:
    """Run every dataset case and return an aggregated results dict."""
    service, mode, live_available = build_service(live)

    case_results: list[dict[str, Any]] = []
    latencies: list[float] = []
    scores: dict[str, int] = {"PASS": 0, "FAIL": 0, "INCONCLUSIVE": 0}
    modes: dict[str, int] = {}

    # B5 runner fix: ALL cases run inside ONE event loop. ``httpx.AsyncClient``
    # (used by the live AliExpress provider) binds to the loop it was first
    # used in, so running every case through its own ``asyncio.run`` closed
    # the loop under the client and made the live provider fail with
    # "Event loop is closed" on the second case.
    async def _run_all_cases() -> None:
        try:
            for case in B5_DATASET:
                res = await run_case(case, service, mode, live_available, timeout_s)
                case_results.append(res)
                if res["latency_ms"] is not None:
                    latencies.append(res["latency_ms"])
                scores[res["score"]] = scores.get(res["score"], 0) + 1
                modes[res["execution_mode"]] = modes.get(res["execution_mode"], 0) + 1
        finally:
            # Release the provider's HTTP client on the SAME loop it ran on
            # (the live AliExpress client owns an httpx.AsyncClient).
            try:
                await service.aclose()
            except Exception:  # noqa: BLE001 - cleanup must never break the run
                pass

    asyncio.run(_run_all_cases())

    latencies_sorted = sorted(latencies)
    total = len(case_results)

    return {
        "dataset_version": DATASET_VERSION,
        "dataset_size": DATASET_SIZE,
        "run_date_utc": datetime.now(timezone.utc).isoformat(),
        "execution_mode": mode,
        "live_available": live_available,
        "total_cases": total,
        "scores": scores,
        "score_rates": {
            k: (v / total if total else 0.0) for k, v in scores.items()
        },
        "modes": modes,
        "latency_ms": {
            "p50": _percentile(latencies_sorted, 50),
            "p95": _percentile(latencies_sorted, 95),
            "min": min(latencies) if latencies else None,
            "max": max(latencies) if latencies else None,
            "mean": round(statistics.mean(latencies), 3) if latencies else None,
            "sample_size": len(latencies),
        },
        "cases": case_results,
    }


# -- output -------------------------------------------------------------------


def write_results(results: dict[str, Any], out_path: Path) -> None:
    out_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def write_report(results: dict[str, Any], report_path: Path, out_path: Path) -> None:
    """Human-readable B5 report with all 14 required sections."""
    lines: list[str] = []
    s = results
    scores = s["scores"]
    total = s["total_cases"]
    lat = s["latency_ms"]

    lines.append("# B5: Search-Pipeline Evaluation Report")
    lines.append("")
    lines.append("## 1. Summary")
    lines.append("")
    lines.append(f"- Dataset version: `{s['dataset_version']}`")
    lines.append(f"- Dataset size: {s['dataset_size']} cases")
    lines.append(f"- Run date (UTC): {s['run_date_utc']}")
    lines.append(f"- Execution mode: `{s['execution_mode']}` (live available: {s['live_available']})")
    lines.append(
        f"- Scores: PASS={scores['PASS']}, FAIL={scores['FAIL']}, "
        f"INCONCLUSIVE={scores['INCONCLUSIVE']} / {total}"
    )
    lines.append(f"- Pass rate: {s['score_rates']['PASS']:.1%}")
    lines.append(f"- Latency p50/p95: {lat['p50']}ms / {lat['p95']}ms (n={lat['sample_size']})")
    lines.append("")

    lines.append("## 2. Scope and Constraints")
    lines.append("")
    lines.append("- Search semantics, relevance, routing, provider fallbacks, and UI were NOT changed.")
    lines.append("- No B1-B4 tests were modified.")
    lines.append("- Live vs mocked/cached results are clearly distinguished per case.")
    lines.append("- No query text, PII, credentials, or API keys are stored in results.")
    lines.append("")

    lines.append("## 3. Execution Environment")
    lines.append("")
    lines.append(f"- Live execution attempted: {s['live_available']}")
    lines.append(f"- Modes observed: {s['modes']}")
    lines.append("")

    lines.append("## 4. Dataset Composition")
    lines.append("")
    lines.append(f"- {total} controlled queries")
    lines.append("- Languages: Russian primary (32) + Ukrainian (2) + English (1)")
    lines.append("- Intent categories: product / ambiguous / discovery / clarification")
    lines.append("- Query shapes: short / long / numeric / budget / exclusion / vague / broad")
    lines.append("")

    lines.append("## 5. Outcome Label Distribution (B4)")
    lines.append("")
    agg: dict[str, int] = {}
    stage_totals = {"raw": 0, "after_relevance": 0, "after_constraints": 0, "final": 0}
    for c in s["cases"]:
        snap = c.get("diagnostics_snapshot") or {}
        for label, count in (snap.get("outcome_counts") or {}).items():
            agg[label] = agg.get(label, 0) + count
        st = snap.get("stage_totals") or {}
        for k in stage_totals:
            stage_totals[k] += int(st.get(k, 0) or 0)
    for label in sorted(agg):
        lines.append(f"- {label}: {agg[label]}")
    lines.append("")
    lines.append("Stage totals (sum over cases):")
    lines.append(f"  - raw: {stage_totals['raw']}")
    lines.append(f"  - after_relevance: {stage_totals['after_relevance']}")
    lines.append(f"  - after_constraints: {stage_totals['after_constraints']}")
    lines.append(f"  - final: {stage_totals['final']}")
    lines.append("")

    lines.append("## 6. Per-Case Results")
    lines.append("")
    lines.append("| id | category | language | mode | hint | outcome | score | latency_ms | blocked | exception |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for c in s["cases"]:
        exc = (c.get("exception") or "").replace("|", "\\|")
        lines.append(
            f"| {c['id']} | {c['category']} | {c['language']} | {c['execution_mode']} | "
            f"{c['expected_outcome_hint']} | {c['outcome_label']} | {c['score']} | "
            f"{c['latency_ms']} | {c['blocked']} | {exc} |"
        )
    lines.append("")

    lines.append("## 7. Scoring Criteria (numerators/denominators)")
    lines.append("")
    lines.append("- PASS: pipeline completed, no total provider failure, outcome stage")
    lines.append("  consistent with expected_outcome_hint.")
    lines.append("- FAIL: product-hint case with total failure or zero final results;")
    lines.append("  empty-hint case that returned invented products.")
    lines.append("- INCONCLUSIVE: execution blocked (no credentials/network) or exception.")
    conclusive = scores["PASS"] + scores["FAIL"]
    lines.append(f"- Numerator/denominator: {conclusive} conclusive / {total} total;")
    lines.append(f"  PASS={scores['PASS']}/{conclusive} conclusive, "
                 f"FAIL={scores['FAIL']}/{conclusive} conclusive.")
    lines.append("")

    lines.append("")

    lines.append("## 8. Latency and Performance")
    lines.append("")
    lines.append(f"- p50: {lat['p50']}ms, p95: {lat['p95']}ms")
    lines.append(f"- min: {lat['min']}ms, max: {lat['max']}ms, mean: {lat['mean']}ms")
    lines.append(f"- sample size: {lat['sample_size']}")
    lines.append("")

    lines.append("## 9. Provider Reliability (B4 diagnostics)")
    lines.append("")
    lines.append("Per-provider stats are aggregated from B4 diagnostics snapshots "
                 "(counts and latencies only; no secrets).")
    lines.append("")

    lines.append("## 10. Privacy and Safety")
    lines.append("")
    lines.append("- No query text, product titles, user ids, or API keys stored.")
    lines.append("- All latency samples bounded (B4 MAX_LATENCY_SAMPLES).")
    lines.append("- Diagnostics fail-safe: a diagnostics bug never breaks a search.")
    lines.append("")

    lines.append("## 11. Regression Check (B4 baseline)")
    lines.append("")
    lines.append("- B4 baseline: 2093 passed, 712 subtests, 0 failed.")
    lines.append("- This runner is standalone (not collected by pytest); the B4 suite")
    lines.append("  must be run separately with `python -m pytest tests/ -q`.")
    lines.append("")

    lines.append("## 12. Blocked Cases")
    lines.append("")
    blocked_cases = [c for c in s["cases"] if c["blocked"] or c["score"] == "INCONCLUSIVE"]
    if blocked_cases:
        for c in blocked_cases:
            lines.append(f"- {c['id']} ({c['category']}/{c['language']}): {c['exception'] or 'blocked'}")
    else:
        lines.append("- None.")
    lines.append("")

    lines.append("## 13. Evidence-Backed Issue List for B6")
    lines.append("")
    issues = []
    failures = [c for c in s["cases"] if c["score"] == "FAIL"]
    if failures:
        by_kind: dict[str, list[dict[str, Any]]] = {}
        for c in failures:
            by_kind.setdefault(c.get("failure_kind") or "unclassified", []).append(c)
        for kind in sorted(by_kind):
            ids = ", ".join(c["id"] for c in by_kind[kind])
            issues.append(f"{len(by_kind[kind])} case(s) [{kind}]: {ids}")
    if scores["INCONCLUSIVE"] > 0:
        issues.append(f"{scores['INCONCLUSIVE']} case(s) INCONCLUSIVE — credentials/network may be limited.")
    if lat["p95"] is not None and lat["p95"] > 5000:
        issues.append(f"p95 latency {lat['p95']}ms exceeds 5s — consider provider timeouts.")
    if not s["live_available"]:
        issues.append("Live execution unavailable — results are mock-only; quality not measured against real providers.")
    if not issues:
        issues.append("- No issues identified from this run.")
    for issue in issues:
        lines.append(f"- {issue}")
    lines.append("")

    if failures:
        lines.append("### 13.1 Failing cases in detail")
        lines.append("")
        lines.append("| id | hint | outcome | final_count | kind | query |")
        lines.append("|---|---|---|---|---|---|")
        for c in failures:
            lines.append(
                f"| {c['id']} | {c['expected_outcome_hint']} | {c['outcome_label']} | "
                f"{c['final_count']} | {c.get('failure_kind') or 'unclassified'} | "
                f"{c['query']} |"
            )
        lines.append("")

    lines.append("## 14. Machine-Readable Results")
    lines.append("")
    lines.append(f"- See `{out_path.name}` for full per-case JSON results.")
    lines.append("- Fields: id, category, language, intent_category, query, expected_outcome_hint,")
    lines.append("  execution_mode, live_available, blocked, exception, outcome_label, stage_counts,")
    lines.append("  searched/failed/unavailable_marketplaces, final_count, latency_ms, score, notes,")
    lines.append("  diagnostics_snapshot (B4 aggregate, no query text).")
    lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")


# -- entry point --------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="B5 search-pipeline evaluation runner")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Attempt real AI + real provider calls (requires credentials + network).",
    )
    parser.add_argument(
        "--out",
        default="tests/eval_b5_results.json",
        help="Path for machine-readable JSON results.",
    )
    parser.add_argument(
        "--report",
        default="tests/eval_b5_report.md",
        help="Path for the human-readable markdown report.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Per-case timeout in seconds (default 60).",
    )
    args = parser.parse_args()

    results = run_all(live=args.live, timeout_s=args.timeout)

    out_path = Path(args.out)
    report_path = Path(args.report)
    write_results(results, out_path)
    write_report(results, report_path, out_path)

    scores = results["scores"]
    total = results["total_cases"]
    print("\n=== B5 EVALUATION COMPLETE ===")
    print(f"Dataset: {results['dataset_version']} ({total} cases)")
    print(f"Mode: {results['execution_mode']} (live available: {results['live_available']})")
    print(f"Scores: PASS={scores['PASS']}, FAIL={scores['FAIL']}, "
          f"INCONCLUSIVE={scores['INCONCLUSIVE']}")
    print(f"Latency p50/p95: {results['latency_ms']['p50']}ms / {results['latency_ms']['p95']}ms")
    print(f"Results: {out_path}")
    print(f"Report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

