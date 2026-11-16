"""Offline contract tests for live-smoke measurement helpers."""

from types import SimpleNamespace

from smoke_conversational_search import (
    _api_delta,
    _gemini_summary,
    _percentile,
    _series_summary,
    _stage_counts,
)


def test_percentile_uses_nearest_rank_and_handles_empty_samples():
    assert _percentile([], 0.95) is None
    assert _percentile([5, 1, 4, 2, 3], 0.50) == 3.0
    assert _percentile([5, 1, 4, 2, 3], 0.95) == 5.0


def test_series_summary_reports_sample_count_and_tail_latency():
    assert _series_summary([]) == {
        "count": 0, "avg_ms": None, "p50_ms": None, "p95_ms": None
    }
    assert _series_summary([10, 20, 30, 40]) == {
        "count": 4, "avg_ms": 25.0, "p50_ms": 20.0, "p95_ms": 40.0
    }


def test_api_delta_separates_logical_calls_retries_and_coalescing():
    before = {
        "aliexpress:0": {
            "calls_requested": 10,
            "calls_ok": 8,
            "calls_err": 1,
            "retries_spent": 2,
            "coalesced_requests": 3,
            "latencies_ms": [5.0, 8.0],
        }
    }
    after = {
        "aliexpress:0": {
            "calls_requested": 14,
            "calls_ok": 10,
            "calls_err": 2,
            "retries_spent": 3,
            "coalesced_requests": 4,
            "latencies_ms": [5.0, 8.0, 12.0, 24.0],
        }
    }

    delta = _api_delta(before, after)

    assert delta["logical_requests"] == 4
    assert delta["successful_calls"] == 2
    assert delta["failed_calls"] == 1
    assert delta["retries"] == 1
    assert delta["coalesced_requests"] == 1
    assert delta["http_attempts"] == 4
    assert delta["latencies_ms"] == [12.0, 24.0]
    assert delta["api_latency"]["p50_ms"] == 12.0
    assert delta["api_latency"]["p95_ms"] == 24.0


def test_gemini_summary_groups_attempts_and_preserves_unknown_usage():
    usage = SimpleNamespace(attempts=[
        SimpleNamespace(
            operation="conversation_turn",
            duration_ms=120.0,
            outcome="ok",
            tokens={"prompt_token_count": 100, "total_token_count": 140},
            cost_usd=0.0001,
        ),
        SimpleNamespace(
            operation="conversation_turn",
            duration_ms=80.0,
            outcome="timeout",
            tokens=None,
            cost_usd=None,
        ),
        SimpleNamespace(
            operation="conversation_selection",
            duration_ms=300.0,
            outcome="ok",
            tokens={"prompt_token_count": 500, "total_token_count": 620},
            cost_usd=0.0004,
        ),
    ])

    summary = _gemini_summary(usage)

    assert summary["attempts"] == 3
    assert summary["by_operation"]["conversation_turn"]["attempts"] == 2
    assert summary["by_operation"]["conversation_turn"]["duration_ms"] == 200.0
    assert summary["by_operation"]["conversation_turn"]["outcomes"] == {
        "ok": 1, "timeout": 1
    }
    assert summary["by_operation"]["conversation_turn"]["tokens"] == {
        "prompt_token_count": 100, "total_token_count": 140
    }
    assert summary["by_operation"]["conversation_turn"]["cost_known_attempts"] == 1
    assert summary["by_operation"]["conversation_selection"]["tokens"]["total_token_count"] == 620


def test_stage_counts_only_exposes_compact_candidate_metrics():
    counts = _stage_counts(
        {
            "raw": 70,
            "hard_rejected": 10,
            "review_eligible": 60,
            "safe_ranked": 42,
            "candidate_pool": 40,
            "candidate_pool_limit": 80,
            "selection_input_count": 40,
            "selection_returned_count": 18,
            "reason": "results",
            "ai_selection": "selected",
            "private_debug_blob": "must not be copied",
        },
        final_count=18,
    )

    assert counts["raw"] == 70
    assert counts["hard_rejected"] == 10
    assert counts["review_eligible"] == 60
    assert counts["final_cards_count"] == 18
    assert "private_debug_blob" not in counts



def test_retrieval_instrumentation_counts_routes_pages_and_wall_time():
    import asyncio

    from smoke_conversational_search import _instrument_retrieval

    class Provider:
        marketplace = "aliexpress"

        async def search_products(self, request):
            return ["real-shaped-result"]

    class SearchService:
        def __init__(self):
            self.providers = [Provider()]

        async def find_products(self, request):
            rows = await self.providers[0].search_products(request)
            return {"request": request, "rows": rows}

    service = SearchService()
    route_calls, durations = _instrument_retrieval(service)
    request = type("Request", (), {"page_no": 2})()
    result = asyncio.run(service.find_products(request))

    assert result["request"] is request
    assert result["rows"] == ["real-shaped-result"]
    assert route_calls == [{"provider": "aliexpress:0", "page_no": 2}]
    assert len(durations) == 1
    assert durations[0] >= 0



def test_count_summary_uses_count_units_not_milliseconds():
    from smoke_conversational_search import _count_summary

    assert _count_summary([]) == {
        "samples": 0, "avg": None, "p50": None, "p95": None
    }
    assert _count_summary([1, 2, 3, 4]) == {
        "samples": 4, "avg": 2.5, "p50": 2.0, "p95": 4.0
    }



def test_custom_live_smoke_query_is_normalized_and_default_matrix_is_preserved():
    from smoke_conversational_search import SEARCH_CASES, _selected_search_cases

    assert _selected_search_cases(None) == SEARCH_CASES
    assert _selected_search_cases("  игровой   ноутбук  ") == (
        ("custom", "игровой ноутбук"),
    )


def test_custom_live_smoke_query_rejects_empty_text():
    import pytest

    from smoke_conversational_search import _selected_search_cases

    with pytest.raises(ValueError, match="non-empty search phrase"):
        _selected_search_cases("   ")
