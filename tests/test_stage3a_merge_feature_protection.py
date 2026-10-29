# -*- coding: utf-8 -*-
"""Stage 3A: the AI merge can never silently erase feature constraints.

``_merge_is_safe`` validated words, budget and identity - but a model answer
that simply OMITTED ``required_features`` / ``excluded_features`` (optional
JSON fields!) constructed the merged request with ``[]`` and erased
constraints the user had already established. The gate now compares both
lists against the deterministic merge of the very same message
(``services.conversation.merge_feature_lists``): established constraints
survive unless the user's own words cancelled them, and a new one only
lands when «с X»/«без X» parsing of that message produced it.

The stubbed model responses mirror the shipped harness
(``RealAIServiceMergeValidationTests``): a real ``AIService`` with the SDK
client faked and ``_generate`` replaced by a JSON fixture - offline, no
network, no Gemini.
"""

from __future__ import annotations

import asyncio
import json
import unittest

from models.search_request import SearchMode, SearchRequest
from services.ai_service import AIService
from services.conversation import _same_feature, resolve_request


def run(coro):
    return asyncio.run(coro)


def parse(text: str) -> SearchRequest:
    """The real deterministic parser - the same one used without Gemini."""

    return run(AIService(api_key=None).parse_query(text))


def previous_with(*, required=(), excluded=()) -> SearchRequest:
    """A settled request carrying already-established feature constraints."""

    return SearchRequest(
        original_query="Нужны беспроводные наушники",
        normalized_query="беспроводные наушники",
        max_price=30.0,
        currency="USD",
        required_features=list(required),
        excluded_features=list(excluded),
        mode=SearchMode.SEARCH,
    )


class MergeFeatureProtectionTests(unittest.TestCase):
    """The AI merge keeps every established constraint, or is refused."""

    BUDGET_TEXT = "а до 20 долларов"

    def setUp(self) -> None:
        self.service = AIService(api_key=None)
        self.service._client = object()  # pretend an SDK is present

    def _with_response(self, payload: dict) -> None:
        async def fake_generate(contents, *, models=None):
            return json.dumps(payload, ensure_ascii=False)

        self.service._generate = fake_generate

    def test_a_missing_required_features_field_cannot_erase_the_constraint(
        self,
    ) -> None:
        previous = previous_with(required=["с микрофоном"])
        self._with_response(
            {"normalized_query": "беспроводные наушники", "max_price": 20.0}
        )
        follow_up = parse(self.BUDGET_TEXT)

        self.assertIsNone(
            run(
                self.service.merge_search_requests(
                    previous, follow_up, self.BUDGET_TEXT
                )
            )
        )

        # The user-visible path still applies the budget through the
        # deterministic fallback - and never loses the constraint.
        effective, refined = run(
            resolve_request(
                previous, follow_up, self.BUDGET_TEXT, ai_service=self.service
            )
        )
        self.assertTrue(refined)
        self.assertEqual(effective.required_features, ["с микрофоном"])
        self.assertEqual(effective.max_price, 20.0)

    def test_a_missing_excluded_features_field_cannot_erase_the_constraint(
        self,
    ) -> None:
        previous = previous_with(excluded=["провод"])
        self._with_response(
            {"normalized_query": "беспроводные наушники", "max_price": 20.0}
        )
        follow_up = parse(self.BUDGET_TEXT)

        self.assertIsNone(
            run(
                self.service.merge_search_requests(
                    previous, follow_up, self.BUDGET_TEXT
                )
            )
        )
        effective, refined = run(
            resolve_request(
                previous, follow_up, self.BUDGET_TEXT, ai_service=self.service
            )
        )
        self.assertTrue(refined)
        self.assertEqual(effective.excluded_features, ["провод"])
        self.assertEqual(effective.max_price, 20.0)

    def test_explicit_empty_lists_do_not_erase_established_constraints(
        self,
    ) -> None:
        previous = previous_with(required=["с микрофоном"], excluded=["провод"])
        self._with_response(
            {
                "normalized_query": "беспроводные наушники",
                "max_price": 20.0,
                "required_features": [],
                "excluded_features": [],
            }
        )
        follow_up = parse(self.BUDGET_TEXT)

        self.assertIsNone(
            run(
                self.service.merge_search_requests(
                    previous, follow_up, self.BUDGET_TEXT
                )
            )
        )

    def test_silently_dropping_one_established_feature_is_rejected(self) -> None:
        previous = previous_with(required=["микрофон", "шумоподавление"])
        self._with_response(
            {
                "normalized_query": "беспроводные наушники",
                "max_price": 20.0,
                "required_features": ["микрофон"],
            }
        )
        follow_up = parse(self.BUDGET_TEXT)

        self.assertIsNone(
            run(
                self.service.merge_search_requests(
                    previous, follow_up, self.BUDGET_TEXT
                )
            )
        )

    def test_a_feature_absent_from_the_message_is_rejected(self) -> None:
        # «водозащита» is in neither the previous request nor the user's
        # message (which carries no «с X»/«без X» at all) - invention, not
        # refinement, so the whole AI merge is refused.
        previous = previous_with(required=["микрофон"])
        self._with_response(
            {
                "normalized_query": "беспроводные наушники",
                "max_price": 20.0,
                "required_features": ["микрофон", "водозащита"],
            }
        )
        follow_up = parse(self.BUDGET_TEXT)

        self.assertIsNone(
            run(
                self.service.merge_search_requests(
                    previous, follow_up, self.BUDGET_TEXT
                )
            )
        )

    def test_a_feature_backed_by_the_user_message_is_accepted(self) -> None:
        # «а с микрофоном» - the parsing of THIS message yields the feature
        # («микрофоном»), so the model's wording «микрофон» is the same
        # constraint and the merge lands.
        text = "а с микрофоном, до 20 долларов"
        previous = previous_with()
        self._with_response(
            {
                "normalized_query": "беспроводные наушники",
                "max_price": 20.0,
                "currency": "USD",
                "required_features": ["микрофон"],
            }
        )
        follow_up = parse(text)

        merged = run(
            self.service.merge_search_requests(previous, follow_up, text)
        )

        self.assertIsNotNone(merged)
        self.assertEqual(merged.max_price, 20.0)
        self.assertEqual(len(merged.required_features), 1)
        self.assertTrue(
            _same_feature(merged.required_features[0], "микрофоном"),
            merged.required_features,
        )

    def test_a_faithful_merge_keeps_constraints_and_budget(self) -> None:
        # The accepted direction of the gate: verbatim lists + a budget the
        # user named -> the AI merge is NOT over-rejected.
        previous = previous_with(required=["с микрофоном"], excluded=["провод"])
        self._with_response(
            {
                "normalized_query": "беспроводные наушники",
                "max_price": 20.0,
                "currency": "USD",
                "required_features": ["с микрофоном"],
                "excluded_features": ["провод"],
            }
        )
        follow_up = parse(self.BUDGET_TEXT)

        merged = run(
            self.service.merge_search_requests(
                previous, follow_up, self.BUDGET_TEXT
            )
        )

        self.assertIsNotNone(merged)
        self.assertEqual(merged.required_features, ["с микрофоном"])
        self.assertEqual(merged.excluded_features, ["провод"])
        self.assertEqual(merged.max_price, 20.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
