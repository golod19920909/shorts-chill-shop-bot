"""Structural checks for the active Option C dialogue evaluation corpus."""

from eval_consultant_dialogue_dataset import (
    DATASET_VERSION,
    DIALOGUE_CASES,
    LANGUAGES,
    validate_dataset,
)


def test_consultant_dialogue_dataset_is_structurally_valid():
    assert validate_dataset() == []


def test_consultant_dialogue_dataset_covers_thirteen_distinct_scenarios():
    assert len(DIALOGUE_CASES) == 13
    assert len({case["scenario"] for case in DIALOGUE_CASES}) == 13
    assert len({case["id"] for case in DIALOGUE_CASES}) == 13


def test_consultant_dialogue_dataset_covers_ru_uk_and_en():
    assert set(LANGUAGES) == {"ru", "uk", "en"}
    assert {case["language"] for case in DIALOGUE_CASES} == {"ru", "uk", "en"}


def test_long_multiturn_case_preserves_assistant_clarification_in_context():
    case = next(item for item in DIALOGUE_CASES if item["id"] == "D13")
    answer_turn = case["turns"][1]
    dialogue = answer_turn["context"]["recent_dialogue"]

    assert dialogue[-1] == {
        "role": "assistant",
        "text": "Для какой задачи нужен компрессор?",
    }
    assert answer_turn["context"]["pending_question"] == "Для какой задачи нужен компрессор?"


def test_provider_failure_and_no_results_are_separate_outcomes():
    no_results = next(item for item in DIALOGUE_CASES if item["id"] == "D11")
    provider_failure = next(item for item in DIALOGUE_CASES if item["id"] == "D12")

    assert no_results["post_search"]["provider_outcome"] == "success_empty_or_no_eligible"
    assert no_results["post_search"]["expected_operations"] == ["no_results_reply"]
    assert provider_failure["post_search"]["provider_outcome"] == "total_failure"
    assert provider_failure["post_search"]["expected_operations"] == []
