"""Stage 44-B7: general negation semantics in the structured-intent layer (regression).

Audit finding (Stage 44-B5 live capture + offline fixtures): the intent SCHEMA
already has two distinct slots - ``required_attributes`` (positive) and
``excluded_product_types`` (things to avoid) - but the AI path sometimes put a
negated phrase («без мешка», «без провода») into ``required_attributes``, where
it is semantically wrong (it is not a spec a title confirms) and pollutes the
retrieval phrase built by ``_retrieval_query``. The deterministic fallback
ALREADY routes «без X» -> ``excluded_product_types`` (Stage 16/22, locked by
tests); this stage makes the AI path CONSISTENT with it via a single guard in
``normalize_intent`` (the one chokepoint for both AI and fallback).

What is fixed and what is honestly NOT (schema limitations):
- A negated phrase never stays a POSITIVE required attribute - it is moved to
  ``excluded_product_types`` (marker stripped, case preserved), matching the
  fallback. Non-destructive: BOTH intent fields feed RANKING only
  (``intent_score_adjustment``), never a hard product filter.
- The IDEAL positive reading of a positive-from-absence («без провода» ->
  «wireless») is NOT produced: that needs an antonym lexicon the project
  deliberately does not add. Excluding the named thing («провода») is the safe,
  schema-supported proxy. This is a documented limitation, not a claimed win.
- The schema has NO slot for a "missing component/software" requirement distinct
  from a product-type exclusion, and NO confidence/ambiguity field - so a
  «ноутбук без ОС» can only be represented as an exclusion of «операционной
  системы» (the existing fallback behavior), never as "laptop lacking an OS".
- Ambiguous negatives are never guessed: a bare negation with no usable noun is
  dropped, never turned into an invented requirement or exclusion.

Offline only: ``normalize_intent`` is a pure function; no Gemini, no AliExpress.
"""

from __future__ import annotations

import unittest

from services.query_intent import StructuredQueryIntent, normalize_intent
from services.text_matching import is_negation_clause, negation_payload


class NegationPayloadTests(unittest.TestCase):
    def test_marker_is_stripped_and_noun_preserved(self) -> None:
        self.assertEqual(negation_payload("без мешка"), "мешка")
        self.assertEqual(negation_payload("без ОС"), "ОС")
        self.assertEqual(negation_payload("не чехол"), "чехол")
        self.assertEqual(negation_payload("without a bag"), "bag")
        self.assertEqual(negation_payload("without ANC"), "ANC")
        self.assertEqual(negation_payload("without the built-in display"),
                         "built-in display")

    def test_non_negation_has_no_payload(self) -> None:
        for phrase in ("беспроводные", "недорогой", "noise cancelling", "1 ТБ",
                       "", None):
            with self.subTest(phrase=phrase):
                self.assertEqual(negation_payload(phrase), "")
                self.assertFalse(is_negation_clause(phrase))


class NegationReclassificationTests(unittest.TestCase):
    def test_absence_phrase_moves_out_of_required_into_excluded(self) -> None:
        # #30 «пылесос без мешка» - the exact live AI answer.
        intent = normalize_intent({
            "product_type": "пылесос",
            "required_attributes": ["без мешка"],
            "excluded_product_types": [],
        })
        self.assertEqual(intent.required_attributes, [])
        self.assertEqual(intent.excluded_product_types, ["мешка"])

    def test_two_negations_stay_separate(self) -> None:
        # #33 «наушники без провода и без шумоподавления» - the model lumped BOTH
        # into required_attributes; both must leave, as two distinct exclusions.
        intent = normalize_intent({
            "product_type": "наушники",
            "required_attributes": ["без провода", "без шумоподавления"],
            "excluded_product_types": [],
        })
        self.assertEqual(intent.required_attributes, [])
        self.assertEqual(
            intent.excluded_product_types, ["провода", "шумоподавления"])

    def test_positive_requirement_and_exclusion_stay_distinct(self) -> None:
        # A mixed list: keep positives, route only the negation. Multiple
        # requirements remain separate.
        intent = normalize_intent({
            "product_type": "wireless earbuds",
            "required_attributes": ["ANC", "multipoint", "without a bag"],
            "excluded_product_types": [],
        })
        self.assertEqual(intent.required_attributes, ["ANC", "multipoint"])
        self.assertEqual(intent.excluded_product_types, ["bag"])

    def test_missing_component_is_not_contradictorily_excluded_from_results(self) -> None:
        # «ноутбук без ОС»: «операционной системы» stays in excluded_product_types
        # (schema limitation: no "lacking OS" slot) and required_attributes stays
        # empty - the OS is never a POSITIVE required attribute.
        intent = normalize_intent({
            "product_type": "ноутбук",
            "required_attributes": [],
            "excluded_product_types": ["операционной системы"],
        })
        self.assertEqual(intent.required_attributes, [])
        self.assertIn("операционной системы", intent.excluded_product_types)

    def test_genuine_exclusion_from_model_is_preserved(self) -> None:
        # #27 «wireless earbuds without ANC» - the model already correct.
        intent = normalize_intent({
            "product_type": "wireless earbuds",
            "required_attributes": [],
            "excluded_product_types": ["ANC"],
        })
        self.assertEqual(intent.required_attributes, [])
        self.assertEqual(intent.excluded_product_types, ["ANC"])

    def test_ambiguous_bare_negation_is_dropped_not_invented(self) -> None:
        # A negation with no usable noun must not become an invented requirement
        # or an empty/garbage exclusion.
        intent = normalize_intent({
            "product_type": "пылесос",
            "required_attributes": ["без"],
            "excluded_product_types": [],
        })
        self.assertEqual(intent.required_attributes, [])
        self.assertEqual(intent.excluded_product_types, [])

    def test_no_duplicate_when_already_excluded(self) -> None:
        # If the noun is already an exclusion, moving must not create a duplicate.
        intent = normalize_intent({
            "product_type": "пылесос",
            "required_attributes": ["без мешка"],
            "excluded_product_types": ["мешка"],
        })
        self.assertEqual(intent.required_attributes, [])
        self.assertEqual(intent.excluded_product_types, ["мешка"])

    def test_pure_positive_specs_untouched(self) -> None:
        # Control: ordinary specs are never reclassified.
        intent = normalize_intent({
            "product_type": "ssd",
            "required_attributes": ["NVMe", "1 ТБ", "M.2"],
            "excluded_product_types": [],
        })
        self.assertEqual(intent.required_attributes, ["NVMe", "1 ТБ", "M.2"])
        self.assertEqual(intent.excluded_product_types, [])


class Stage44B6IntactTests(unittest.TestCase):
    def test_parse_query_required_features_negation_guard_unaffected(self) -> None:
        # The Stage 44-B6 fix (ai_service._to_search_request dropping negations
        # from required_features) is a DIFFERENT field and must remain intact.
        from services.ai_service import AIService
        request = AIService._to_search_request(
            {"original_query": "пылесос без мешка",
             "normalized_query": "bagless vacuum cleaner",
             "required_features": ["без мешка"], "excluded_features": []},
            fallback_text="пылесос без мешка",
        )
        self.assertEqual(request.required_features, [])
        self.assertEqual(request.excluded_features, [])

    def test_schema_still_forbids_unknown_fields(self) -> None:
        # The intent model keeps extra="forbid": no new field was smuggled in.
        with self.assertRaises(Exception):
            StructuredQueryIntent(invented_field="x")


if __name__ == "__main__":
    unittest.main()

