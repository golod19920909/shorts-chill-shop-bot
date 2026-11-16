"""LIVE / API-COSTING smoke test for the Option C shopping consultant.

Requires local AliExpress and Gemini credentials. It makes real marketplace
requests and Gemini model calls, consuming API quota/usage and potentially
incurring model charges. It is a manual diagnostic only and is never executed
by CI.

Run manually from the project root (uses the local .env):

    .venv/Scripts/python.exe smoke_conversational_search.py
    .venv/Scripts/python.exe smoke_conversational_search.py --max-search-pages 1
    .venv/Scripts/python.exe smoke_conversational_search.py --repeat 3

By default, the smoke uses the SearchService's configured page depth. The
optional page-depth flag overrides it. Repetitions default to one; increase
them explicitly when a meaningful p50/p95 sample is needed.

Requires both ALIEXPRESS_APP_KEY/SECRET and GEMINI_API_KEY. It runs eight
broad Russian-language searches across different product families through
the real ConversationSearchEngine, AliExpress Affiliate API and Gemini, then
asks a shown-products comparison question without starting another marketplace
search. It reports per-search wall time, actual Affiliate API request/attempt
counts and call latency, Gemini attempts/tokens/cost by operation, candidate
stage counts, final cards and no-result reason. It never prints credentials
or affiliate URLs.

This is an intentionally small live smoke, not a benchmark and not a claim
that eight queries represent the whole AliExpress catalogue. The broader
cross-category regression matrix remains in the offline test suite.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import statistics
import sys
import time
from collections import Counter, defaultdict

from bot import build_search_service
from config.settings import load_settings
from services.convsearch.contract import Outcome
from services.convsearch.engine import ConversationSearchEngine
from services.convsearch.state import ConversationEngineState
from services.gemini_telemetry import turn_scope
from services.product_assistant import ProductAssistant


SEARCH_CASES = (
    ("power_tool", "аккумуляторный шуруповёрт"),
    ("portable_power", "портативный аккумулятор"),
    ("water_filter", "фильтр для воды под мойку"),
    ("security_camera", "уличная камера видеонаблюдения"),
    ("wireless_audio", "беспроводные наушники"),
    ("vacuum_cleaner", "пылесос без мешка"),
    ("garden_equipment", "садовый шланг для полива"),
    ("lighting", "налобный фонарь"),
)

def _selected_search_cases(custom_query: str | None) -> tuple[tuple[str, str], ...]:
    if custom_query is None:
        return SEARCH_CASES
    normalized = " ".join(custom_query.split())
    if not normalized:
        raise ValueError("--query must contain a non-empty search phrase")
    return (("custom", normalized),)


STAGE_FIELDS = (
    "raw",
    "hard_rejected",
    "review_eligible",
    "safe_ranked",
    "after_type",
    "after_feature",
    "after_exclusion",
    "after_budget",
    "after_dedup",
    "ranked",
    "candidate_pool",
    "candidate_pool_limit",
    "selection_input_count",
    "selection_requested_count",
    "selection_returned_count",
    "selection_validated_count",
    "final_cards_count",
)


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return round(ordered[index], 1)


def _series_summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "avg_ms": None, "p50_ms": None, "p95_ms": None}
    return {
        "count": len(values),
        "avg_ms": round(statistics.fmean(values), 1),
        "p50_ms": _percentile(values, 0.50),
        "p95_ms": _percentile(values, 0.95),
    }


def _count_summary(values: list[int]) -> dict[str, float | int | None]:
    if not values:
        return {"samples": 0, "avg": None, "p50": None, "p95": None}
    return {
        "samples": len(values),
        "avg": round(statistics.fmean(values), 1),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
    }


def _api_snapshot(search_service) -> dict[str, dict]:
    """Read only the safe counters from provider clients; never request data."""
    snapshot = {}
    for index, provider in enumerate(getattr(search_service, "providers", ())):
        client = getattr(provider, "_client", None)
        if client is None:
            continue
        marketplace = getattr(provider, "marketplace", None)
        label = str(getattr(marketplace, "value", marketplace) or type(provider).__name__)
        key = f"{label}:{index}"
        snapshot[key] = {
            "calls_requested": int(getattr(client, "calls_requested", 0) or 0),
            "calls_ok": int(getattr(client, "calls_ok", 0) or 0),
            "calls_err": int(getattr(client, "calls_err", 0) or 0),
            "retries_spent": int(getattr(client, "retries_spent", 0) or 0),
            "coalesced_requests": int(getattr(client, "coalesced_requests", 0) or 0),
            "latencies_ms": list(getattr(client, "call_latencies_ms", ()) or ()),
        }
    return snapshot


def _api_delta(before: dict[str, dict], after: dict[str, dict]) -> dict:
    totals = {
        "logical_requests": 0,
        "successful_calls": 0,
        "failed_calls": 0,
        "retries": 0,
        "coalesced_requests": 0,
        "http_attempts": 0,
        "latencies_ms": [],
        "providers": {},
    }
    for key, current in after.items():
        previous = before.get(key, {})
        delta = {
            field: max(0, int(current.get(field, 0)) - int(previous.get(field, 0)))
            for field in (
                "calls_requested",
                "calls_ok",
                "calls_err",
                "retries_spent",
                "coalesced_requests",
            )
        }
        old_latencies = previous.get("latencies_ms", [])
        new_latencies = current.get("latencies_ms", [])
        if len(new_latencies) >= len(old_latencies):
            latencies = new_latencies[len(old_latencies):]
        else:  # bounded deque rolled over; retain the available new sample
            latencies = new_latencies
        row = {
            "logical_requests": delta["calls_requested"],
            "successful_calls": delta["calls_ok"],
            "failed_calls": delta["calls_err"],
            "retries": delta["retries_spent"],
            "coalesced_requests": delta["coalesced_requests"],
            "http_attempts": delta["calls_ok"] + delta["calls_err"] + delta["retries_spent"],
            "api_latency": _series_summary(latencies),
        }
        totals["providers"][key] = row
        for field in (
            "logical_requests", "successful_calls", "failed_calls",
            "retries", "coalesced_requests", "http_attempts",
        ):
            totals[field] += row[field]
        totals["latencies_ms"].extend(latencies)
    totals["api_latency"] = _series_summary(totals["latencies_ms"])
    return totals


def _gemini_summary(usage) -> dict:
    by_operation: dict[str, dict] = {}
    for attempt in getattr(usage, "attempts", ()):
        operation = str(getattr(attempt, "operation", "unknown"))
        row = by_operation.setdefault(
            operation,
            {
                "attempts": 0,
                "duration_ms": 0.0,
                "outcomes": {},
                "tokens": defaultdict(int),
                "known_cost_usd": 0.0,
                "cost_known_attempts": 0,
            },
        )
        row["attempts"] += 1
        row["duration_ms"] += float(getattr(attempt, "duration_ms", 0.0) or 0.0)
        outcome = str(getattr(attempt, "outcome", "unknown"))
        row["outcomes"][outcome] = row["outcomes"].get(outcome, 0) + 1
        tokens = getattr(attempt, "tokens", None) or {}
        for name, value in tokens.items():
            if isinstance(value, int):
                row["tokens"][name] += value
        cost = getattr(attempt, "cost_usd", None)
        if cost is not None:
            row["known_cost_usd"] += float(cost)
            row["cost_known_attempts"] += 1

    result = {}
    for operation, row in by_operation.items():
        result[operation] = {
            "attempts": row["attempts"],
            "duration_ms": round(row["duration_ms"], 1),
            "outcomes": row["outcomes"],
            "tokens": dict(row["tokens"]),
            "known_cost_usd": round(row["known_cost_usd"], 10),
            "cost_known_attempts": row["cost_known_attempts"],
        }
    return {
        "attempts": sum(row["attempts"] for row in result.values()),
        "by_operation": result,
    }


def _stage_counts(report: dict, final_count: int) -> dict:
    counts = {field: report.get(field) for field in STAGE_FIELDS if field in report}
    counts["final_cards_count"] = int(report.get("final_cards_count", final_count))
    counts["reason"] = report.get("reason")
    counts["ai_selection"] = report.get("ai_selection", "not-run")
    return counts


def _retrieval_summary(route_calls: list[dict], durations_ms: list[float]) -> dict:
    pages = sorted({
        int(call["page_no"])
        for call in route_calls
        if isinstance(call.get("page_no"), int) and call["page_no"] > 0
    })
    return {
        "provider_route_calls": len(route_calls),
        "pages_requested": pages,
        "unique_pages_requested": len(pages),
        "retrieval_calls": len(durations_ms),
        "retrieval_wall_ms": round(sum(durations_ms), 1),
    }


def _instrument_retrieval(search_service):
    """Time the real SearchService/provider boundaries without changing results."""
    route_calls: list[dict] = []
    retrieval_durations_ms: list[float] = []

    for index, provider in enumerate(getattr(search_service, "providers", ())):
        original_search = provider.search_products
        marketplace = getattr(provider, "marketplace", None)
        label = str(getattr(marketplace, "value", marketplace) or type(provider).__name__)

        async def timed_provider_search(request, _original=original_search, _label=label, _index=index):
            route_calls.append({
                "provider": f"{_label}:{_index}",
                "page_no": int(getattr(request, "page_no", 1) or 1),
            })
            return await _original(request)

        provider.search_products = timed_provider_search

    original_find_products = search_service.find_products

    async def timed_retrieval(*args, **kwargs):
        started = time.perf_counter()
        try:
            return await original_find_products(*args, **kwargs)
        finally:
            retrieval_durations_ms.append((time.perf_counter() - started) * 1000)

    search_service.find_products = timed_retrieval
    return route_calls, retrieval_durations_ms


def _sample_products(products) -> str:
    lines = []
    for product in products[:5]:
        price = (
            f"{product.price:g} {product.currency}"
            if product.price is not None and product.currency
            else "цена не указана"
        )
        lines.append(
            f"  - {product.title[:100]} | {price} | "
            f"affiliate={'yes' if product.affiliate_url else 'no'}"
        )
    return "\n".join(lines) if lines else "  - нет подходящих карточек"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-search-pages",
        type=int,
        default=None,
        help="override the configured AliExpress page depth (default: runtime setting)",
    )
    parser.add_argument(
        "--query",
        default=None,
        help="run one controlled query instead of the default eight-case matrix",
    )
    parser.add_argument(
        "--search-only",
        action="store_true",
        help="skip shown-product follow-up and comparison calls (lower API/model usage)",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        choices=range(1, 6),
        metavar="{1,2,3,4,5}",
        help="repeat the eight-case matrix for a more useful p50/p95 sample (default: 1)",
    )
    args = parser.parse_args()
    if args.max_search_pages is not None and args.max_search_pages < 1:
        parser.error("--max-search-pages must be at least 1")
    try:
        search_cases = _selected_search_cases(args.query)
    except ValueError as exc:
        parser.error(str(exc))

    settings = load_settings()
    if not settings.aliexpress.is_configured:
        print("SMOKE SKIPPED: ALIEXPRESS_APP_KEY/SECRET not configured")
        return 0
    if not settings.gemini_api_key:
        print("SMOKE SKIPPED: GEMINI_API_KEY not configured")
        return 0

    search_service, ai_service = build_search_service(settings)
    if args.max_search_pages is not None:
        search_service.max_search_pages = args.max_search_pages
    print(
        "AliExpress conversational page depth: "
        f"{search_service.max_search_pages}; repetitions={args.repeat}; "
        f"cases={len(search_cases)}; search_only={args.search_only}"
    )
    engine = ConversationSearchEngine()
    engine.bind_search_service(search_service)
    engine.bind_ai_service(ai_service)
    assistant = ProductAssistant(ai_service=ai_service, search_service=search_service)
    provider_route_calls, retrieval_durations_ms = _instrument_retrieval(search_service)
    failures: list[str] = []
    warnings: list[str] = []
    search_rows: list[dict] = []
    followup_rows: list[dict] = []

    try:
        for repetition in range(1, args.repeat + 1):
            for label, query in search_cases:
                user_key = f"live-smoke-{repetition}-{label}"
                state = ConversationEngineState(user_key=user_key)
                print(f"\n=== {label} (run {repetition}/{args.repeat}): {query} ===")
                api_before = _api_snapshot(search_service)
                route_start = len(provider_route_calls)
                retrieval_start = len(retrieval_durations_ms)
                search_started = time.perf_counter()
                with turn_scope(f"live-smoke-search-{repetition}-{label}") as search_usage:
                    try:
                        decision = await engine.handle_message(
                            user_key,
                            query,
                            state=state,
                            currency="USD",
                            result_limit=30,
                        )
                    except Exception as exc:  # noqa: BLE001 - smoke must report a safe verdict
                        wall_ms = (time.perf_counter() - search_started) * 1000
                        api_delta = _api_delta(api_before, _api_snapshot(search_service))
                        search_rows.append({
                            "label": label,
                            "repetition": repetition,
                            "wall_ms": wall_ms,
                            "api": api_delta,
                            "retrieval": _retrieval_summary(
                                provider_route_calls[route_start:],
                                retrieval_durations_ms[retrieval_start:],
                            ),
                            "gemini": _gemini_summary(search_usage),
                            "stages": {"reason": f"engine_exception:{type(exc).__name__}"},
                        })
                        failures.append(f"{label}: engine exception {type(exc).__name__}")
                        print(f"FAIL: engine exception {type(exc).__name__}")
                        continue
                wall_ms = (time.perf_counter() - search_started) * 1000
                api_delta = _api_delta(api_before, _api_snapshot(search_service))
                retrieval_delta = _retrieval_summary(
                    provider_route_calls[route_start:],
                    retrieval_durations_ms[retrieval_start:],
                )
                gemini_delta = _gemini_summary(search_usage)
                stages = _stage_counts(decision.report, len(decision.products))
                search_rows.append({
                    "label": label,
                    "repetition": repetition,
                    "wall_ms": wall_ms,
                    "api": api_delta,
                    "retrieval": retrieval_delta,
                    "gemini": gemini_delta,
                    "stages": stages,
                })

                print(
                    f"Outcome={decision.outcome.value}; products={len(decision.products)}; "
                    f"AI-selection={stages['ai_selection']}; reason={stages['reason']}"
                )
                print(_sample_products(decision.products))
                print(
                    "METRICS "
                    f"wall_ms={wall_ms:.1f} "
                    f"api_requests={api_delta['logical_requests']} "
                    f"api_attempts={api_delta['http_attempts']} "
                    f"api_retries={api_delta['retries']} "
                    f"api_latency_p50/p95="
                    f"{api_delta['api_latency']['p50_ms']}/"
                    f"{api_delta['api_latency']['p95_ms']} "
                    f"retrieval_ms={retrieval_delta['retrieval_wall_ms']:.1f} "
                    f"routes={retrieval_delta['provider_route_calls']} "
                    f"pages={retrieval_delta['pages_requested']} "
                    f"gemini_attempts={gemini_delta['attempts']}"
                )
                print(
                    "STAGES "
                    + " ".join(f"{key}={value}" for key, value in stages.items())
                )
                for operation, metrics in gemini_delta["by_operation"].items():
                    print(
                        f"GEMINI operation={operation} attempts={metrics['attempts']} "
                        f"duration_ms={metrics['duration_ms']:.1f} "
                        f"tokens={metrics['tokens']} "
                        f"known_cost_usd={metrics['known_cost_usd']:.10f} "
                        f"cost_known_attempts={metrics['cost_known_attempts']} "
                        f"outcomes={metrics['outcomes']}"
                    )

                if decision.outcome is not Outcome.RESULTS or not decision.products:
                    failures.append(f"{label}: no result cards ({decision.outcome.value})")
                    continue

                ids = [product.product_id for product in decision.products]
                if len(ids) != len(set(ids)):
                    failures.append(f"{label}: duplicate product IDs in final result")
                if len(decision.products) > 30:
                    failures.append(f"{label}: presentation cap exceeded")
                if any(not product.affiliate_url for product in decision.products):
                    failures.append(f"{label}: one or more results lack affiliate links")
                if stages["ai_selection"] in {
                    "timeout_fallback",
                    "error_fallback",
                    "unavailable_fallback",
                    "turn_budget_exhausted",
                    "semantic_review_unavailable",
                }:
                    warnings.append(
                        f"{label}: Gemini selection degraded ({stages['ai_selection']})"
                    )

                if args.search_only:
                    continue

                # Follow-up and comparison work is measured separately from
                # the search request so it cannot inflate search wall time.
                with turn_scope(f"live-smoke-followup-{repetition}-{label}") as followup_usage:
                    followup_started = time.perf_counter()
                    state.record_shown(ids)
                    compare = await engine.handle_message(
                        user_key,
                        "Какой из них лучше?",
                        state=state,
                        shown_products=decision.products,
                        currency="USD",
                        result_limit=30,
                    )
                    advice = None
                    if compare.outcome is Outcome.COMPARE:
                        advice = await assistant.answer(
                            "Сравни два варианта и объясни, чем они отличаются.",
                            decision.products,
                        )
                    followup_ms = (time.perf_counter() - followup_started) * 1000
                followup_metrics = _gemini_summary(followup_usage)
                followup_rows.append({
                    "label": label,
                    "repetition": repetition,
                    "wall_ms": followup_ms,
                    "gemini": followup_metrics,
                    "compare_outcome": compare.outcome.value,
                    "comparison_answer": bool(advice and advice.text.strip()),
                })
                print(
                    f"Follow-up outcome={compare.outcome.value}; "
                    f"wall_ms={followup_ms:.1f}; gemini_attempts={followup_metrics['attempts']}"
                )
                for operation, metrics in followup_metrics["by_operation"].items():
                    print(
                        f"FOLLOWUP GEMINI operation={operation} attempts={metrics['attempts']} "
                        f"duration_ms={metrics['duration_ms']:.1f} "
                        f"tokens={metrics['tokens']} "
                        f"known_cost_usd={metrics['known_cost_usd']:.10f} "
                        f"cost_known_attempts={metrics['cost_known_attempts']}"
                    )
                if compare.outcome is not Outcome.COMPARE:
                    failures.append(f"{label}: shown-product question did not route to COMPARE")
                    continue
                if advice is None or not advice.text.strip():
                    failures.append(f"{label}: product comparison returned no answer")
                    print("Comparison: no grounded answer available")
                else:
                    print(f"Comparison: {advice.text[:500]}")
                    if "Вариант 1" not in advice.text or "Вариант 2" not in advice.text:
                        failures.append(f"{label}: comparison did not render two distinct options")

    finally:
        await search_service.aclose()

    wall_values = [row["wall_ms"] for row in search_rows]
    api_latency_values = [
        value
        for row in search_rows
        for value in row["api"]["latencies_ms"]
    ]
    api_attempts = sum(row["api"]["http_attempts"] for row in search_rows)
    api_requests = sum(row["api"]["logical_requests"] for row in search_rows)
    retrieval_wall_values = [row["retrieval"]["retrieval_wall_ms"] for row in search_rows]
    provider_route_counts = [row["retrieval"]["provider_route_calls"] for row in search_rows]
    unique_page_counts = [row["retrieval"]["unique_pages_requested"] for row in search_rows]
    api_request_counts = [row["api"]["logical_requests"] for row in search_rows]
    api_attempt_counts = [row["api"]["http_attempts"] for row in search_rows]
    operation_durations: dict[str, list[float]] = defaultdict(list)
    operation_tokens: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    operation_costs: dict[str, float] = defaultdict(float)
    operation_cost_known_attempts: dict[str, int] = defaultdict(int)
    reason_counts = Counter(
        row["stages"].get("reason") or "unknown" for row in search_rows
    )
    for row in search_rows:
        for operation, metrics in row["gemini"]["by_operation"].items():
            operation_durations[operation].append(metrics["duration_ms"])
            for name, value in metrics["tokens"].items():
                operation_tokens[operation][name] += value

    print("\n=== LIVE PERFORMANCE SUMMARY ===")
    print(f"Search wall time p50/p95: {_series_summary(wall_values)}")
    print(f"SearchService retrieval wall time p50/p95: {_series_summary(retrieval_wall_values)}")
    print(f"Affiliate API call latency p50/p95: {_series_summary(api_latency_values)}")
    print(
        "Per-search API requests p50/p95: "
        f"{_count_summary(api_request_counts)}; HTTP attempts p50/p95: "
        f"{_count_summary(api_attempt_counts)}"
    )
    print(
        "Per-search provider routes p50/p95: "
        f"{_count_summary(provider_route_counts)}; unique pages p50/p95: "
        f"{_count_summary(unique_page_counts)}"
    )
    print(f"Affiliate API logical requests={api_requests}; HTTP attempts including retries={api_attempts}")
    for operation, durations in sorted(operation_durations.items()):
        print(
            f"Gemini {operation}: duration p50/p95={_series_summary(durations)}; "
            f"tokens_total={dict(operation_tokens[operation])}; "
            f"known_cost_usd_total={operation_costs[operation]:.10f}; "
            f"cost_known_attempts={operation_cost_known_attempts[operation]}"
        )
    print(f"No-result / decision reasons: {dict(reason_counts)}")
    if followup_rows:
        print(
            "Follow-up/comparison wall time p50/p95: "
            f"{_series_summary([row['wall_ms'] for row in followup_rows])}"
        )
        followup_durations: dict[str, list[float]] = defaultdict(list)
        followup_tokens: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        followup_costs: dict[str, float] = defaultdict(float)
        for row in followup_rows:
            for operation, metrics in row["gemini"]["by_operation"].items():
                followup_durations[operation].append(metrics["duration_ms"])
                followup_costs[operation] += metrics["known_cost_usd"]
                for name, value in metrics["tokens"].items():
                    followup_tokens[operation][name] += value
        for operation, durations in sorted(followup_durations.items()):
            print(
                f"Follow-up Gemini {operation}: duration p50/p95={_series_summary(durations)}; "
                f"tokens_total={dict(followup_tokens[operation])}; "
                f"known_cost_usd_total={followup_costs[operation]:.10f}"
            )

    if warnings:
        print("\nDEGRADED BUT RESILIENT:")
        for warning in warnings:
            print(f"  - {warning}")
    print("\nSMOKE OK" if not failures else f"\nSMOKE FAILED: {failures}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
