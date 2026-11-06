"""Offline unit checks for the blind consultant benchmark runner."""

import asyncio
import json

from tests import eval_consultant_benchmark_runner as benchmark
from tests.eval_consultant_benchmark_runner import (
    TURN_SCHEMA,
    ModelResponse,
    Provider,
    _cost_usd,
    _extract_openai_text,
    _google_request,
    _qwen_url,
    score_decision,
    validate_provider_roster,
    Provider,
)


def test_benchmark_schema_requires_the_complete_interpreter_contract():
    assert set(TURN_SCHEMA["required"]) == {
        "action", "confidence", "product_type", "marketplace_terms",
        "question", "response", "slot_updates",
    }
    assert TURN_SCHEMA["additionalProperties"] is False


def test_score_decision_checks_action_type_slots_and_retrieval():
    expected = {
        "action": "correction",
        "product_type": None,
        "slot_updates": [{
            "operation": "replace",
            "kind": "feature",
            "target": "two batteries",
            "value": "one battery",
        }],
        "retrieval": True,
    }
    actual = {
        "action": "correction",
        "product_type": None,
        "slot_updates": [{
            "operation": "replace",
            "kind": "feature",
            "target": " Two batteries ",
            "value": "one battery",
        }],
    }

    score = score_decision(expected, actual)
    assert score["accepted"] is True
    assert score["all_exact"] is True


def test_score_decision_rejects_wrong_product_switch_and_slot_loss():
    expected = {
        "action": "new_search",
        "product_type": "wireless headphones",
        "slot_updates": [{
            "operation": "add",
            "kind": "feature",
            "target": None,
            "value": "noise cancelling",
        }],
        "retrieval": True,
    }
    actual = {
        "action": "refine",
        "product_type": None,
        "slot_updates": [],
    }

    score = score_decision(expected, actual)
    assert score["action_exact"] is False
    assert score["product_type_exact"] is False
    assert score["slot_updates_exact"] is False
    assert score["retrieval_exact"] is True
    assert score["all_exact"] is False


def test_qwen_endpoint_uses_supplied_workspace_base_without_guessing():
    assert _qwen_url("https://workspace.ap-southeast-1.maas.aliyuncs.com") == (
        "https://workspace.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1/chat/completions"
    )
    assert _qwen_url("https://workspace.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1") == (
        "https://workspace.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1/chat/completions"
    )


def test_cost_formula_prices_cached_input_separately_without_double_counting():
    cost = _cost_usd(
        {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 200},
        {
            "input_per_million": 0.75,
            "cached_input_per_million": 0.075,
            "output_per_million": 3.75,
        },
    )
    assert cost == 0.00123


def test_openai_responses_text_extractor_collects_output_text_blocks():
    payload = {
        "output": [
            {"content": [{"type": "output_text", "text": "{\"action\":\"respond\"}"}]},
        ]
    }
    assert _extract_openai_text(payload) == "{\"action\":\"respond\"}"


def test_benchmark_requires_two_distinct_providers_not_two_google_models():
    google_models = [
        Provider("gemini", "google", "gemini-3.8-flash", "test-key"),
        Provider("gemini_lite", "google", "gemini-3.1-flash-lite", "test-key"),
    ]
    try:
        validate_provider_roster(google_models)
    except SystemExit as exc:
        assert "two distinct providers" in str(exc)
    else:
        raise AssertionError("Google-only model comparison must not count as cross-provider")


def test_cost_formula_prices_cache_writes_as_a_separate_input_class():
    cost = _cost_usd(
        {
            "input_tokens": 1000,
            "cached_input_tokens": 400,
            "cache_write_tokens": 100,
            "output_tokens": 200,
        },
        {
            "input_per_million": 0.75,
            "cached_input_per_million": 0.075,
            "cache_write_per_million": 0.125,
            "output_per_million": 3.75,
        },
    )
    assert cost == 0.0011675


def test_gemini_baseline_uses_the_same_prompt_only_json_mode_as_production():
    body = _google_request("exact prompt")

    assert body == {"contents": [{"parts": [{"text": "exact prompt"}]}]}
    assert "generationConfig" not in body


def test_benchmark_results_do_not_reveal_provider_identity(tmp_path, monkeypatch):
    async def fake_call_provider(provider, prompt, timeout_seconds):
        return ModelResponse(
            text=json.dumps({
                "action": "clarify",
                "confidence": 0.9,
                "product_type": None,
                "marketplace_terms": [],
                "question": "Какой именно товар вам нужен?",
                "response": None,
                "slot_updates": [],
            }, ensure_ascii=False),
            usage={
                "input_tokens": 10,
                "output_tokens": 20,
                "reasoning_tokens": 0,
                "cached_input_tokens": 0,
            },
            duration_ms=5.0,
            http_status=200,
            resolved_model=provider.model,
        )

    monkeypatch.setattr(benchmark, "call_provider", fake_call_provider)
    providers = [
        Provider("provider_a", "openai", "model-a", "key-a"),
        Provider("provider_b", "anthropic", "model-b", "key-b"),
    ]
    results_path = tmp_path / "blind-results.json"
    map_path = tmp_path / "private-map.json"

    asyncio.run(benchmark.run_benchmark(
        providers,
        output_path=results_path,
        map_path=map_path,
        repeat=1,
        timeout_seconds=4.0,
        seed=123,
        rates={
            "model-a": {"input_per_million": 0.1, "output_per_million": 0.5},
            "model-b": {"input_per_million": 0.1, "output_per_million": 0.5},
        },
    ))

    results = json.loads(results_path.read_text(encoding="utf-8"))
    private_map = json.loads(map_path.read_text(encoding="utf-8"))
    assert "seed" not in results["manifest"]
    assert "rates_by_model" not in results["manifest"]
    assert results["results"]
    for row in results["results"]:
        assert "provider" not in row
        assert "model" not in row
        assert "resolved_model" not in row
        assert "billing_model_id" not in row
    assert set(private_map["providers"]) == {"A", "B"}
    assert private_map["rates_by_model"]
    assert private_map["seed"] == 123
