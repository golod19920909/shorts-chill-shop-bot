"""Offline structural checks for auxiliary consultant evaluation cases."""

from tests.eval_consultant_auxiliary_dataset import (
    CHAT_ACTION_CASES,
    NO_RESULTS_CASES,
    validate_dataset,
)


def test_auxiliary_dataset_has_unique_multilingual_cases():
    validate_dataset()
    assert len(CHAT_ACTION_CASES) >= 12
    assert len(NO_RESULTS_CASES) >= 10
    assert {case["language"] for case in CHAT_ACTION_CASES} == {"ru", "uk", "en"}
    assert {case["language"] for case in NO_RESULTS_CASES} == {"ru", "uk", "en"}


def test_no_results_corpus_never_treats_provider_failure_as_empty_catalogue():
    assert all(case["reason"] in {"no_candidates", "no_eligible"} for case in NO_RESULTS_CASES)
    assert all("provider_failure" not in case["reason"] for case in NO_RESULTS_CASES)


def test_short_clarification_corpus_contains_both_actions():
    assert {case["expected_action"] for case in CHAT_ACTION_CASES} == {"search", "clarify"}
    assert all(case["text"].strip() for case in CHAT_ACTION_CASES)
