"""Stage 53: anaphoric follow-ups must not drop the product identity.

The Stage 52 live defect: after a screwdriver search, the follow-up
«Найди вариант с двумя аккумуляторами, но не дороже 40 долларов» was parsed
into ``2 batteries два аккумулятора`` and started a NEW search for batteries.
Both merge paths (deterministic fallback and the AI-merge acceptance gate in
``resolve_request``) must keep the previous product unless the user explicitly
names a different one. Category-independent: no «шуруповёрт»/«аккумулятор»
word is hardcoded anywhere in the fix.
"""

from __future__ import annotations

import asyncio

import pytest

from services.ai_service import AIService
from services.conversation import (
    deterministic_merge,
    is_refinement,
    resolve_request,
)

STEP1 = "Помоги выбрать хороший шуруповёрт для дома."
STEP2 = "В основном для сборки мебели и иногда для дерева. Бюджет до 40 долларов."
STEP3 = "Найди вариант с двумя аккумуляторами, но не дороже 40 долларов."
CHEAPER = "Подешевле."
SWITCH = "Забудь шуруповёрт, найди набор отвёрток"
SWITCH2 = "Теперь найди беспроводные наушники"


def _parse(text: str):
    return AIService._fallback_parse(text)


class _FakeAI:
    """Mimics the AI merge, returning whatever parsed request we hand it."""

    def __init__(self, parsed):
        self.is_available = True
        self._parsed = parsed

    async def merge_search_requests(self, previous, follow_up, raw_text):
        return self._parsed


def _names_screwdriver(request) -> bool:
    return "шурупов" in (request.normalized_query or "")


# --- Stage 52 sequence: product identity survives every follow-up ---------


def test_step1_is_a_fresh_search():
    request = _parse(STEP1)
    assert _names_screwdriver(request)


def test_step2_use_and_budget_refine_without_new_product():
    previous = _parse(STEP1)
    follow_up = _parse(STEP2)
    assert is_refinement(previous, follow_up, STEP2) is True

    merged, refined = deterministic_merge(previous, follow_up, STEP2)
    assert refined is True
    assert _names_screwdriver(merged)
    assert merged.max_price == 40.0


def test_step3_anaphoric_variant_keeps_the_screwdriver():
    previous = _parse(STEP1)
    after2, _ = deterministic_merge(previous, _parse(STEP2), STEP2)

    follow_up = _parse(STEP3)
    # «вариант»/«найди» are scaffolding; «с двумя аккумуляторами» is a feature.
    assert is_refinement(after2, follow_up, STEP3) is True

    merged, refined = deterministic_merge(after2, follow_up, STEP3)
    assert refined is True
    # The product the user originally asked for is preserved.
    assert _names_screwdriver(merged)
    # The two-battery requirement is added to the SAME product, not searched alone.
    assert any("аккумулятор" in feature for feature in merged.required_features)
    # The previously stated budget is preserved.
    assert merged.max_price == 40.0


def test_step3_ai_merge_drift_to_batteries_is_rejected():
    """The exact Stage 52 failure, through resolve_request's acceptance gate."""
    previous = _parse(STEP1)
    after2, _ = deterministic_merge(previous, _parse(STEP2), STEP2)
    follow_up = _parse(STEP3)

    # A model that DRIFTED and returned a batteries-only request.
    drifted = _parse("2 batteries два аккумулятора")
    effective, refined = asyncio.run(
        resolve_request(after2, follow_up, STEP3, ai_service=_FakeAI(drifted))
    )
    assert refined is True
    assert _names_screwdriver(effective)


# --- «Подешевле» keeps refining without inventing a budget -----------------


def test_podeshevle_refines_without_inventing_budget():
    previous = _parse(STEP1)
    after2, _ = deterministic_merge(previous, _parse(STEP2), STEP2)
    after3, _ = deterministic_merge(after2, _parse(STEP3), STEP3)

    merged, refined = deterministic_merge(after3, _parse(CHEAPER), CHEAPER)
    assert refined is True
    assert _names_screwdriver(merged)
    # No numeric budget is invented; the $40 limit stays.
    assert merged.max_price == 40.0


# --- explicit product switches still reset ---------------------------------


@pytest.mark.parametrize(
    "switch, new_product",
    [
        (SWITCH, "отверт"),
        (SWITCH2, "наушник"),
    ],
)
def test_explicit_switch_starts_new_search(switch, new_product):
    previous = _parse(STEP1)
    after2, _ = deterministic_merge(previous, _parse(STEP2), STEP2)
    follow_up = _parse(switch)
    merged, refined = deterministic_merge(after2, follow_up, switch)
    # A switch is a new search, and the NEW product is what gets searched.
    # («Забудь шуруповёрт…» literally names the old product in order to drop
    # it, so we assert on the requested product rather than the old word.)
    assert refined is False
    query = (merged.normalized_query or "").lower().replace("ё", "е")
    assert new_product.replace("ё", "е") in query
