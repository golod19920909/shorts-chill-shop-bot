"""Offline unit checks for the blind consultant benchmark runner."""

from tests.eval_consultant_benchmark_runner import (
    TURN_SCHEMA,
    _cost_usd,
    _extract_openai_text,
    _qwen_url,
    score_decision,
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
