"""Multi-turn search refinement on top of the EXISTING search pipeline.

The state lives in ``context.user_data`` (per user, in memory, expiring) and
only holds the last effective ``SearchRequest`` - no new table, no migration
and nothing sensitive. These tests cover the nine required scenarios, with the
AI either absent, available or returning something unusable.
"""

from __future__ import annotations

import asyncio
import time
import unittest

from models.search_request import SearchMode, SearchRequest
from services.ai_service import AIService
from services.conversation import (
    CONTEXT_TTL_SECONDS,
    ConversationState,
    deterministic_merge,
    is_refinement,
    resolve_request,
)


def run(coro):
    return asyncio.run(coro)


def parse(text: str) -> SearchRequest:
    """The real deterministic parser - the same one used without Gemini."""

    return run(AIService(api_key=None).parse_query(text))


INITIAL_TEXT = "Нужны беспроводные наушники до $30"


class ConversationStateTests(unittest.TestCase):
    def test_a_fresh_state_is_not_expired(self) -> None:
        state = ConversationState(request=parse(INITIAL_TEXT))

        self.assertFalse(state.is_expired())

    def test_state_expires_and_is_dropped(self) -> None:
        state = ConversationState(request=parse(INITIAL_TEXT))
        state.updated_at = time.monotonic() - CONTEXT_TTL_SECONDS - 1

        self.assertTrue(state.is_expired())

    def test_touch_extends_the_lifetime(self) -> None:
        state = ConversationState(request=parse(INITIAL_TEXT))
        state.updated_at = time.monotonic() - CONTEXT_TTL_SECONDS - 1

        state.touch()

        self.assertFalse(state.is_expired())


class RefinementDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous = parse(INITIAL_TEXT)

    def test_a_qualifying_addition_is_a_refinement(self) -> None:
        follow_up = parse("а с хорошим микрофоном")

        self.assertTrue(
            is_refinement(self.previous, follow_up, "а с хорошим микрофоном")
        )

    def test_a_new_product_is_a_new_search(self) -> None:
        follow_up = parse("чехол для телефона")

        self.assertFalse(
            is_refinement(self.previous, follow_up, "чехол для телефона")
        )

    def test_a_new_price_alone_refines(self) -> None:
        self.assertTrue(
            is_refinement(self.previous, parse("а до 20 долларов"), "а до 20 долларов")
        )


class DeterministicMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous = parse(INITIAL_TEXT)

    def test_initial_search_has_no_previous_context(self) -> None:
        request = parse(INITIAL_TEXT)

        self.assertEqual(request.max_price, 30.0)
        self.assertEqual(request.currency, "USD")

    def test_adding_a_feature_keeps_the_product_and_the_budget(self) -> None:
        text = "а с хорошим микрофоном"

        merged, refined = deterministic_merge(self.previous, parse(text), text)

        self.assertTrue(refined)
        self.assertIn("микрофон", " ".join(merged.required_features).lower())
        self.assertEqual(merged.max_price, 30.0)
        self.assertEqual(merged.normalized_query, self.previous.normalized_query)

    def test_changing_the_price_replaces_the_previous_limit(self) -> None:
        text = "а до 20 долларов"

        merged, refined = deterministic_merge(self.previous, parse(text), text)

        self.assertTrue(refined)
        self.assertEqual(merged.max_price, 20.0)

    def test_removing_a_feature_clears_the_requirement(self) -> None:
        first = "а с микрофоном"
        with_mic, _ = deterministic_merge(
            self.previous, parse(first), first
        )
        second = "без микрофона"

        merged, refined = deterministic_merge(with_mic, parse(second), second)

        self.assertTrue(refined)
        self.assertIn("микрофон", " ".join(merged.excluded_features).lower())
        self.assertFalse(
            any(
                "микрофон" in feature.lower()
                for feature in merged.required_features
            )
        )

    def test_changing_the_category_is_a_new_search(self) -> None:
        follow_up = SearchRequest(
            original_query="а в категории Авто",
            normalized_query="автоаксессуары",
            category="auto",
        )

        merged, refined = deterministic_merge(self.previous, follow_up, "а в категории Авто")

        self.assertFalse(refined)
        self.assertEqual(merged, follow_up)

    def test_an_unrelated_search_replaces_the_context(self) -> None:
        text = "чехол для телефона"

        merged, refined = deterministic_merge(self.previous, parse(text), text)

        self.assertFalse(refined)
        self.assertEqual(merged.normalized_query, parse(text).normalized_query)
        self.assertNotIn(
            "наушники", (merged.normalized_query or "").lower()
        )

    def test_clearing_the_price_removes_the_limit(self) -> None:
        text = "а любая цена"

        merged, refined = deterministic_merge(self.previous, parse(text), text)

        self.assertTrue(refined)
        self.assertIsNone(merged.max_price)

    def test_the_mode_stays_a_plain_search(self) -> None:
        text = "а с хорошим микрофоном"

        merged, _ = deterministic_merge(self.previous, parse(text), text)

        self.assertEqual(merged.mode, SearchMode.SEARCH)


class ResolveRequestTests(unittest.TestCase):
    """AI may refine a merge, but only with known words and known values."""

    def setUp(self) -> None:
        self.previous = parse(INITIAL_TEXT)

    def test_without_a_previous_request_the_message_is_used_as_is(self) -> None:
        follow_up = parse(INITIAL_TEXT)

        effective, refined = run(resolve_request(None, follow_up, INITIAL_TEXT))

        self.assertFalse(refined)
        self.assertEqual(effective, follow_up)

    def test_ai_unavailable_falls_back_to_the_deterministic_merge(self) -> None:
        text = "а с хорошим микрофоном"
        follow_up = parse(text)

        effective, refined = run(
            resolve_request(self.previous, follow_up, text, ai_service=None)
        )

        self.assertTrue(refined)
        self.assertEqual(effective.normalized_query, self.previous.normalized_query)
        self.assertIn(
            "микрофон", " ".join(effective.required_features).lower()
        )

    def test_malformed_ai_response_falls_back_to_the_deterministic_merge(
        self,
    ) -> None:
        class BrokenAI:
            is_available = True

            async def merge_search_requests(self, previous, follow_up, raw_text):
                return "не объект"

        text = "а до 20 долларов"
        follow_up = parse(text)

        effective, refined = run(
            resolve_request(self.previous, follow_up, text, ai_service=BrokenAI())
        )

        self.assertTrue(refined)
        self.assertEqual(effective.max_price, 20.0)

    def test_ai_cannot_introduce_an_unknown_product(self) -> None:
        class DriftingAI:
            is_available = True

            async def merge_search_requests(self, previous, follow_up, raw_text):
                return SearchRequest(
                    original_query=raw_text,
                    normalized_query="кроссовки",
                    max_price=30.0,
                )

        text = "а с хорошим микрофоном"
        follow_up = parse(text)

        effective, refined = run(
            resolve_request(self.previous, follow_up, text, ai_service=DriftingAI())
        )
        # The drifted «кроссовки» must never reach the search...
        self.assertNotIn("кроссовки", (effective.normalized_query or "").lower())
        # ...and a feature-only follow-up must refine the ORIGINAL product
        # rather than drop it: the headphones stay, the microphone is added.
        # (Genuine product switches are covered by UnrelatedProductAfterASearch.)
        self.assertTrue(refined)
        self.assertEqual(effective.normalized_query, self.previous.normalized_query)
        self.assertIn("микрофон", " ".join(effective.required_features).lower())

    def test_ai_cannot_invent_a_budget(self) -> None:
        # The guard is the real service's job (see the class below), so the
        # shipped validation is exercised with a stubbed model response.
        service = AIService(api_key=None)
        service._client = object()

        async def invent(contents, *, models=None):
            return (
                '{"normalized_query": "беспроводные наушники", '
                '"max_price": 999.0}'
            )

        service._generate = invent
        previous = parse(INITIAL_TEXT)
        text = "а с хорошим микрофоном"

        merged = run(
            service.merge_search_requests(previous, parse(text), text)
        )

        self.assertIsNone(merged)


class RealAIServiceMergeValidationTests(unittest.TestCase):
    """The shipped guard, exercised with a stubbed model response."""

    def setUp(self) -> None:
        self.service = AIService(api_key=None)
        self.service._client = object()  # pretend an SDK is present

    def _with_response(self, payload: dict):
        import json

        async def fake_generate(contents, *, models=None):
            return json.dumps(payload, ensure_ascii=False)

        self.service._generate = fake_generate
        return self.service

    def test_a_safe_merge_is_accepted(self) -> None:
        service = self._with_response(
            {
                "normalized_query": "беспроводные наушники",
                "max_price": 20.0,
                "currency": "USD",
                "required_features": ["микрофон"],
            }
        )
        previous = parse(INITIAL_TEXT)
        text = "а с микрофоном, до 20 долларов"

        merged = run(
            service.merge_search_requests(previous, parse(text), text)
        )

        self.assertIsNotNone(merged)
        self.assertEqual(merged.max_price, 20.0)

    def test_an_unknown_number_is_rejected(self) -> None:
        service = self._with_response(
            {"normalized_query": "беспроводные наушники", "max_price": 999.0}
        )
        previous = parse(INITIAL_TEXT)
        text = "а с хорошим микрофоном"

        self.assertIsNone(
            run(service.merge_search_requests(previous, parse(text), text))
        )

    def test_unknown_words_are_rejected(self) -> None:
        service = self._with_response(
            {"normalized_query": "беспроводные наушники кроссовки"}
        )
        previous = parse(INITIAL_TEXT)
        text = "а с хорошим микрофоном"

        self.assertIsNone(
            run(service.merge_search_requests(previous, parse(text), text))
        )

    def test_malformed_model_output_is_rejected(self) -> None:
        async def broken(contents, *, models=None):
            return "простите, я не понимаю"

        self.service._generate = broken
        previous = parse(INITIAL_TEXT)
        text = "а с хорошим микрофоном"

        self.assertIsNone(
            run(
                self.service.merge_search_requests(previous, parse(text), text)
            )
        )

    def test_no_client_means_no_merge(self) -> None:
        service = AIService(api_key=None)
        previous = parse(INITIAL_TEXT)
        text = "а с хорошим микрофоном"

        self.assertIsNone(
            run(service.merge_search_requests(previous, parse(text), text))
        )


class UnrelatedProductAfterASearchTests(unittest.TestCase):
    """The reported «Плазморезка» answered with the previous search's product.

    Root cause: a merge that keeps the PREVIOUS catalog phrase and parks the
    new product word in ``keywords`` (a shape a model really produced) looked
    like a refinement of the old search, so the pipeline searched the OLD
    product and the user got a watch strap as the answer to a query about a
    plasma cutter. The user's own message decides now - not where a parser or a
    model parked the words.
    """

    def setUp(self) -> None:
        self.previous = SearchRequest(
            original_query="ремешок часов",
            normalized_query="watch strap",
            mode=SearchMode.SEARCH,
        )

    @staticmethod
    def _ai(merged: SearchRequest):
        class LeakyAI:
            is_available = True

            async def merge_search_requests(self, previous, follow_up, raw_text):
                return merged

        return LeakyAI()

    def _leaked(self, raw_text: str) -> SearchRequest:
        """The merge shape that caused the production failure."""

        return SearchRequest(
            original_query=raw_text,
            normalized_query=self.previous.normalized_query,
            keywords=[raw_text],
            mode=SearchMode.SEARCH,
        )

    def test_the_new_product_replaces_the_previous_search(self) -> None:
        text = "плазморезка"

        effective, refined = run(
            resolve_request(
                self.previous, parse(text), text, ai_service=self._ai(self._leaked(text))
            )
        )

        self.assertFalse(refined)
        self.assertEqual(effective.normalized_query, "плазморезка")
        self.assertNotIn("watch strap", (effective.normalized_query or "").lower())

    def test_the_leak_is_refused_even_with_a_connector(self) -> None:
        text = "а плазморезка?"

        effective, refined = run(
            resolve_request(
                self.previous, parse(text), text, ai_service=self._ai(self._leaked(text))
            )
        )

        self.assertFalse(refined)
        self.assertNotIn("strap", (effective.normalized_query or "").lower())

    def test_the_detection_reads_the_users_own_words(self) -> None:
        text = "плазморезка"

        self.assertFalse(is_refinement(self.previous, self._leaked(text), text))

    def test_a_real_refinement_of_the_same_product_still_works(self) -> None:
        text = "а с микрофоном, до 20 долларов"
        merged = SearchRequest(
            original_query=text,
            normalized_query=self.previous.normalized_query,
            required_features=["микрофон"],
            max_price=20.0,
            mode=SearchMode.SEARCH,
        )

        effective, refined = run(
            resolve_request(self.previous, parse(text), text, ai_service=self._ai(merged))
        )

        self.assertTrue(refined)
        self.assertEqual(effective.max_price, 20.0)
        self.assertEqual(effective.normalized_query, self.previous.normalized_query)


class ColourAndBudgetRefinementRegressionTests(unittest.TestCase):
    """Two reproduced release defects, pinned at the real decision path.

    Both drive the REAL deterministic parser and the REAL
    ``conversation.is_refinement`` - the exact production code that decides
    refinement-vs-reset - so a regression here is the user-visible bug, not a
    mocked expectation:

    * a colour refinement («в красном цвете») must REFINE the current product,
      not switch to a product literally called «красном» (it used to reset and
      drop the case/phone the user was refining);
    * a budget-adverb refinement («Только до 40 долларов» / «желательно до 20»)
      must REFINE and apply the ceiling, not treat «только»/«желательно» as a
      new product.

    The genuine-switch guards prove the fix is not a blanket "always refine":
    a colour word next to a real product noun («красные кроссовки») and a
    colour-named product («зелёный чай») still correctly reset.
    """

    def test_colour_refinement_preserves_the_product(self) -> None:
        previous = parse("Ищу чехол для Samsung S26")
        text = "Найди вариант в красном цвете"
        follow_up = parse(text)

        self.assertTrue(is_refinement(previous, follow_up, text))
        # The colour never becomes the product query.
        self.assertNotIn("красном", (follow_up.normalized_query or "").split()[:1])

    def test_colour_refinement_merges_and_keeps_the_case_query(self) -> None:
        previous = parse("Ищу чехол для Samsung S26")
        text = "Найди вариант в красном цвете"
        merged, refined = run(resolve_request(previous, parse(text), text))

        self.assertTrue(refined)
        self.assertIn("чехол", (merged.normalized_query or "").lower())

    def test_budget_adverb_refinement_applies_the_ceiling(self) -> None:
        previous = parse("Нужны беспроводные наушники с шумоподавлением")
        text = "Только до 40 долларов"
        merged, refined = run(resolve_request(previous, parse(text), text))

        self.assertTrue(refined)
        self.assertEqual(merged.max_price, 40.0)
        # The headphones product is preserved, not replaced by «только».
        self.assertIn("наушник", (merged.normalized_query or "").lower())

    def test_colour_noun_variants_all_refine(self) -> None:
        previous = parse("Ищу чехол для Samsung S26")
        for text in ("в красном цвете", "красного цвета", "а в синем цвете"):
            with self.subTest(text=text):
                self.assertTrue(is_refinement(previous, parse(text), text))

    def test_genuine_switch_still_resets(self) -> None:
        previous = parse("ремешок для часов")
        for text in ("плазморезка", "красные кроссовки", "зелёный чай"):
            with self.subTest(text=text):
                self.assertFalse(is_refinement(previous, parse(text), text))


if __name__ == "__main__":
    unittest.main()
