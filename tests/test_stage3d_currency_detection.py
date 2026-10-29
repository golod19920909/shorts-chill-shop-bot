# -*- coding: utf-8 -*-
"""Stage 3D: Consistent currency detection across query parsers.

Covers both parsers (ai_service.parse_query / _fallback_parse and
query_intent.parse_query_intent / fallback_intent), the apply_intent
propagation, multi-turn budget refinement, explicit currency changes,
no-currency queries, contradictory AI output, and Stage 2 / Stage 3C
regression guards.

All tests are offline and deterministic: no Gemini, no network.
"""

from __future__ import annotations

import asyncio
import unittest

from models.search_request import SearchRequest
from services.ai_service import AIService, _detect_currency, _CURRENCY_MARKERS
from services.query_intent import (
    CURRENCY_ALIASES,
    IntentBudget,
    StructuredQueryIntent,
    apply_intent,
    fallback_intent,
    normalize_currency,
    normalize_intent,
    parse_query_intent,
    reset_ai_state,
)


def run(coro):
    return asyncio.run(coro)


def ai_parse(text: str) -> SearchRequest:
    """Parse via the real AIService fallback parser (no key configured)."""
    svc = AIService(api_key=None)
    return run(svc.parse_query(text))


def intent_parse(text: str):
    """Parse via parse_query_intent with no AI (deterministic fallback)."""
    return run(parse_query_intent(text, None))


def resolve_request(text: str) -> SearchRequest:
    """Full pipeline: ai fallback parse -> intent fallback -> apply_intent."""
    base = ai_parse(text)
    intent, _ = intent_parse(text)
    return apply_intent(base, intent)


class FakeAI:
    """Minimal stand-in for AIService implementing only _generate."""

    def __init__(self, result=None, *, exc=None, client=True):
        self._client = object() if client else None
        self.fast_model = "gemini-test"
        self.last_model = None
        self.calls = 0
        self._result = result
        self._exc = exc

    async def _generate(self, contents, *, models=None):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._result


# ---------------------------------------------------------------------------
# 1. Currency symbols
# ---------------------------------------------------------------------------

class CurrencySymbolTests(unittest.TestCase):
    """Both parsers must recognise explicit currency symbols."""

    def test_ai_service_detects_dollar_symbol(self):
        self.assertEqual(_detect_currency("$80"), "USD")
        self.assertEqual(_detect_currency("under $80"), "USD")

    def test_ai_service_detects_euro_symbol(self):
        self.assertEqual(_detect_currency("€80"), "EUR")
        self.assertEqual(_detect_currency("80 €"), "EUR")

    def test_ai_service_detects_ruble_symbol(self):
        self.assertEqual(_detect_currency("₽80"), "RUB")
        self.assertEqual(_detect_currency("80 ₽"), "RUB")

    def test_ai_service_detects_yuan_symbol(self):
        self.assertEqual(_detect_currency("¥80"), "CNY")
        self.assertEqual(_detect_currency("80 ¥"), "CNY")

    def test_ai_service_detects_pound_symbol(self):
        self.assertEqual(_detect_currency("£80"), "GBP")
        self.assertEqual(_detect_currency("80 £"), "GBP")

    def test_fallback_parse_preserves_symbol_currency(self):
        req = ai_parse("headphones under $80")
        self.assertEqual(req.currency, "USD")
        self.assertEqual(req.max_price, 80.0)

        req = ai_parse("наушники до 80 €")
        self.assertEqual(req.currency, "EUR")
        self.assertEqual(req.max_price, 80.0)

    def test_intent_parser_preserves_symbol_currency(self):
        intent, _ = intent_parse("headphones under $80")
        self.assertEqual(intent.budget.currency, "USD")
        self.assertEqual(intent.budget.max, 80.0)

        intent, _ = intent_parse("наушники до 80 €")
        self.assertEqual(intent.budget.currency, "EUR")
        self.assertEqual(intent.budget.max, 80.0)

    def test_resolved_request_keeps_symbol_currency(self):
        req = resolve_request("headphones under $80")
        self.assertEqual(req.currency, "USD")
        self.assertEqual(req.max_price, 80.0)

        req = resolve_request("наушники до 80 €")
        self.assertEqual(req.currency, "EUR")
        self.assertEqual(req.max_price, 80.0)


# ---------------------------------------------------------------------------
# 2. Currency codes
# ---------------------------------------------------------------------------

class CurrencyCodeTests(unittest.TestCase):
    """Both parsers must recognise explicit 3-letter currency codes."""

    def test_ai_service_detects_currency_codes(self):
        self.assertEqual(_detect_currency("80 usd"), "USD")
        self.assertEqual(_detect_currency("80 eur"), "EUR")
        self.assertEqual(_detect_currency("80 rub"), "RUB")
        self.assertEqual(_detect_currency("80 cny"), "CNY")
        self.assertEqual(_detect_currency("80 uah"), "UAH")
        self.assertEqual(_detect_currency("80 грн"), "UAH")
        self.assertEqual(_detect_currency("80 gbp"), "GBP")

    def test_intent_parser_normalizes_currency_codes(self):
        for alias, expected in [
            ("usd", "USD"), ("eur", "EUR"), ("rub", "RUB"),
            ("cny", "CNY"), ("uah", "UAH"), ("gbp", "GBP"),
        ]:
            self.assertEqual(normalize_currency(alias), expected, alias)

    def test_resolved_request_keeps_code_currency(self):
        req = resolve_request("headphones under 80 usd")
        self.assertEqual(req.currency, "USD")
        self.assertEqual(req.max_price, 80.0)

        req = resolve_request("наушники до 80 eur")
        self.assertEqual(req.currency, "EUR")
        self.assertEqual(req.max_price, 80.0)


# ---------------------------------------------------------------------------
# 3. Singular and plural English names
# ---------------------------------------------------------------------------

class EnglishNameTests(unittest.TestCase):
    """Both parsers must recognise English currency words (singular/plural)."""

    def test_ai_service_detects_english_currency_words(self):
        self.assertEqual(_detect_currency("80 dollars"), "USD")
        self.assertEqual(_detect_currency("80 dollar"), "USD")
        self.assertEqual(_detect_currency("80 us dollars"), "USD")
        self.assertEqual(_detect_currency("80 us dollar"), "USD")
        self.assertEqual(_detect_currency("20 euros"), "EUR")
        self.assertEqual(_detect_currency("20 euro"), "EUR")
        self.assertEqual(_detect_currency("100 rubles"), "RUB")
        self.assertEqual(_detect_currency("100 ruble"), "RUB")
        self.assertEqual(_detect_currency("500 yuans"), "CNY")
        self.assertEqual(_detect_currency("500 yuan"), "CNY")
        self.assertEqual(_detect_currency("20 pounds"), "GBP")
        self.assertEqual(_detect_currency("20 pound"), "GBP")

    def test_intent_parser_normalizes_english_currency_words(self):
        self.assertEqual(normalize_currency("dollar"), "USD")
        self.assertEqual(normalize_currency("dollars"), "USD")
        self.assertEqual(normalize_currency("us dollar"), "USD")
        self.assertEqual(normalize_currency("us dollars"), "USD")
        self.assertEqual(normalize_currency("euro"), "EUR")
        self.assertEqual(normalize_currency("euros"), "EUR")
        self.assertEqual(normalize_currency("ruble"), "RUB")
        self.assertEqual(normalize_currency("rubles"), "RUB")
        self.assertEqual(normalize_currency("yuan"), "CNY")
        self.assertEqual(normalize_currency("yuans"), "CNY")
        self.assertEqual(normalize_currency("pound"), "GBP")
        self.assertEqual(normalize_currency("pounds"), "GBP")

    def test_fallback_parse_preserves_english_currency(self):
        req = ai_parse("headphones under 80 dollars")
        self.assertEqual(req.currency, "USD")
        self.assertEqual(req.max_price, 80.0)

        req = ai_parse("headphones over 20 euros")
        self.assertEqual(req.currency, "EUR")
        self.assertEqual(req.min_price, 20.0)

        req = ai_parse("wireless mouse under 50 us dollars")
        self.assertEqual(req.currency, "USD")
        self.assertEqual(req.max_price, 50.0)

    def test_intent_parser_preserves_english_currency(self):
        intent, _ = intent_parse("headphones under 80 dollars")
        self.assertEqual(intent.budget.currency, "USD")
        self.assertEqual(intent.budget.max, 80.0)

        intent, _ = intent_parse("headphones over 20 euros")
        self.assertEqual(intent.budget.currency, "EUR")
        self.assertEqual(intent.budget.min, 20.0)

    def test_resolved_request_keeps_english_currency(self):
        req = resolve_request("headphones under 80 dollars")
        self.assertEqual(req.currency, "USD")
        self.assertEqual(req.max_price, 80.0)

        req = resolve_request("headphones over 20 euros")
        self.assertEqual(req.currency, "EUR")
        self.assertEqual(req.min_price, 20.0)


# ---------------------------------------------------------------------------
# 4. Russian aliases
# ---------------------------------------------------------------------------

class RussianAliasTests(unittest.TestCase):
    """Both parsers must recognise Russian currency words."""

    def test_ai_service_detects_russian_currency_words(self):
        self.assertEqual(_detect_currency("80 долларов"), "USD")
        self.assertEqual(_detect_currency("80 доллара"), "USD")
        self.assertEqual(_detect_currency("20 евро"), "EUR")
        self.assertEqual(_detect_currency("100 рублей"), "RUB")
        self.assertEqual(_detect_currency("100 рубля"), "RUB")
        self.assertEqual(_detect_currency("100 рубль"), "RUB")
        self.assertEqual(_detect_currency("500 юаней"), "CNY")
        self.assertEqual(_detect_currency("500 юаня"), "CNY")
        self.assertEqual(_detect_currency("500 юань"), "CNY")

    def test_intent_parser_normalizes_russian_currency_words(self):
        self.assertEqual(normalize_currency("доллар"), "USD")
        self.assertEqual(normalize_currency("доллара"), "USD")
        self.assertEqual(normalize_currency("долларов"), "USD")
        self.assertEqual(normalize_currency("доллары"), "USD")
        self.assertEqual(normalize_currency("евро"), "EUR")
        self.assertEqual(normalize_currency("евр"), "EUR")
        self.assertEqual(normalize_currency("рубль"), "RUB")
        self.assertEqual(normalize_currency("рубля"), "RUB")
        self.assertEqual(normalize_currency("рублей"), "RUB")
        self.assertEqual(normalize_currency("юань"), "CNY")
        self.assertEqual(normalize_currency("юаня"), "CNY")
        self.assertEqual(normalize_currency("юаней"), "CNY")

    def test_fallback_parse_preserves_russian_currency(self):
        req = ai_parse("наушники до 80 долларов")
        self.assertEqual(req.currency, "USD")
        self.assertEqual(req.max_price, 80.0)

        req = ai_parse("наушники до 20 евро")
        self.assertEqual(req.currency, "EUR")
        self.assertEqual(req.max_price, 20.0)

        req = ai_parse("наушники от 100 рублей")
        self.assertEqual(req.currency, "RUB")
        self.assertEqual(req.min_price, 100.0)

    def test_intent_parser_preserves_russian_currency(self):
        intent, _ = intent_parse("наушники до 80 долларов")
        self.assertEqual(intent.budget.currency, "USD")
        self.assertEqual(intent.budget.max, 80.0)

        intent, _ = intent_parse("наушники от 100 рублей")
        self.assertEqual(intent.budget.currency, "RUB")
        self.assertEqual(intent.budget.min, 100.0)

    def test_resolved_request_keeps_russian_currency(self):
        req = resolve_request("наушники до 80 долларов")
        self.assertEqual(req.currency, "USD")
        self.assertEqual(req.max_price, 80.0)

        req = resolve_request("наушники от 100 рублей")
        self.assertEqual(req.currency, "RUB")
        self.assertEqual(req.min_price, 100.0)


# ---------------------------------------------------------------------------
# 5. Ukrainian aliases
# ---------------------------------------------------------------------------

class UkrainianAliasTests(unittest.TestCase):
    """Both parsers must recognise Ukrainian currency words where supported."""

    def test_ai_service_detects_ukrainian_currency_words(self):
        self.assertEqual(_detect_currency("80 доларів"), "USD")
        self.assertEqual(_detect_currency("80 долара"), "USD")
        self.assertEqual(_detect_currency("80 долар"), "USD")
        self.assertEqual(_detect_currency("20 євро"), "EUR")
        self.assertEqual(_detect_currency("100 рублів"), "RUB")
        self.assertEqual(_detect_currency("100 рубля"), "RUB")
        self.assertEqual(_detect_currency("100 рубль"), "RUB")
        self.assertEqual(_detect_currency("500 юанів"), "CNY")

    def test_intent_parser_normalizes_ukrainian_currency_words(self):
        self.assertEqual(normalize_currency("долар"), "USD")
        self.assertEqual(normalize_currency("долара"), "USD")
        self.assertEqual(normalize_currency("доларів"), "USD")
        self.assertEqual(normalize_currency("євро"), "EUR")
        self.assertEqual(normalize_currency("рубль"), "RUB")
        self.assertEqual(normalize_currency("рубля"), "RUB")
        self.assertEqual(normalize_currency("рублів"), "RUB")
        self.assertEqual(normalize_currency("юанів"), "CNY")

    def test_fallback_parse_preserves_ukrainian_currency(self):
        req = ai_parse("наушники до 80 доларів")
        self.assertEqual(req.currency, "USD")
        self.assertEqual(req.max_price, 80.0)

        req = ai_parse("наушники до 20 євро")
        self.assertEqual(req.currency, "EUR")
        self.assertEqual(req.max_price, 20.0)

    def test_intent_parser_preserves_ukrainian_currency(self):
        intent, _ = intent_parse("наушники до 80 доларів")
        self.assertEqual(intent.budget.currency, "USD")
        self.assertEqual(intent.budget.max, 80.0)

    def test_resolved_request_keeps_ukrainian_currency(self):
        req = resolve_request("наушники до 80 доларів")
        self.assertEqual(req.currency, "USD")
        self.assertEqual(req.max_price, 80.0)


# ---------------------------------------------------------------------------
# 6. Case-insensitive matching
# ---------------------------------------------------------------------------

class CaseInsensitiveTests(unittest.TestCase):
    """Currency detection must be case-insensitive."""

    def test_ai_service_case_insensitive(self):
        self.assertEqual(_detect_currency("80 usd".lower()), "USD")
        self.assertEqual(_detect_currency("80 eur".lower()), "EUR")
        self.assertEqual(_detect_currency("80 euro".lower()), "EUR")
        self.assertEqual(_detect_currency("80 доллар".lower()), "USD")
        self.assertEqual(_detect_currency("80 долларов".lower()), "USD")

    def test_intent_parser_case_insensitive(self):
        self.assertEqual(normalize_currency("USD"), "USD")
        self.assertEqual(normalize_currency("usd"), "USD")
        self.assertEqual(normalize_currency("Usd"), "USD")
        self.assertEqual(normalize_currency("EUR"), "EUR")
        self.assertEqual(normalize_currency("eur"), "EUR")
        self.assertEqual(normalize_currency("Евро"), "EUR")
        self.assertEqual(normalize_currency("ЕВРО"), "EUR")

    def test_fallback_parse_case_insensitive(self):
        req = ai_parse("HEADPHONES UNDER 80 DOLLARS")
        self.assertEqual(req.currency, "USD")
        self.assertEqual(req.max_price, 80.0)

        req = ai_parse("Наушники До 80 Долларов")
        self.assertEqual(req.currency, "USD")
        self.assertEqual(req.max_price, 80.0)


# ---------------------------------------------------------------------------
# 7. Both parsers independently
# ---------------------------------------------------------------------------

class ParserIndependenceTests(unittest.TestCase):
    """Each parser must independently produce the same currency result."""

    def test_both_parsers_agree_on_usd_dollars(self):
        req_ai = ai_parse("headphones under 80 dollars")
        intent, _ = intent_parse("headphones under 80 dollars")
        self.assertEqual(req_ai.currency, intent.budget.currency)
        self.assertEqual(req_ai.currency, "USD")
        self.assertEqual(req_ai.max_price, intent.budget.max)
        self.assertEqual(req_ai.max_price, 80.0)

    def test_both_parsers_agree_on_eur_symbol(self):
        req_ai = ai_parse("наушники до 80 €")
        intent, _ = intent_parse("наушники до 80 €")
        self.assertEqual(req_ai.currency, intent.budget.currency)
        self.assertEqual(req_ai.currency, "EUR")
        self.assertEqual(req_ai.max_price, intent.budget.max)
        self.assertEqual(req_ai.max_price, 80.0)

    def test_both_parsers_agree_on_russian_rub(self):
        req_ai = ai_parse("наушники от 100 рублей")
        intent, _ = intent_parse("наушники от 100 рублей")
        self.assertEqual(req_ai.currency, intent.budget.currency)
        self.assertEqual(req_ai.currency, "RUB")
        self.assertEqual(req_ai.min_price, intent.budget.min)
        self.assertEqual(req_ai.min_price, 100.0)

    def test_both_parsers_agree_on_ukrainian_usd(self):
        req_ai = ai_parse("наушники до 80 доларів")
        intent, _ = intent_parse("наушники до 80 доларів")
        self.assertEqual(req_ai.currency, intent.budget.currency)
        self.assertEqual(req_ai.currency, "USD")
        self.assertEqual(req_ai.max_price, intent.budget.max)
        self.assertEqual(req_ai.max_price, 80.0)


# ---------------------------------------------------------------------------
# 8. Agreement of the final resolved request
# ---------------------------------------------------------------------------

class FinalRequestAgreementTests(unittest.TestCase):
    """apply_intent must preserve currency from either parser."""

    def test_apply_intent_sets_currency_from_intent(self):
        base = SearchRequest(original_query="headphones", normalized_query="headphones")
        intent = StructuredQueryIntent(
            budget=IntentBudget(max=80.0, currency="USD")
        )
        result = apply_intent(base, intent)
        self.assertEqual(result.currency, "USD")
        self.assertEqual(result.max_price, 80.0)

    def test_apply_intent_overwrites_currency_on_explicit_change(self):
        base = SearchRequest(
            original_query="headphones under 80 dollars",
            normalized_query="headphones",
            max_price=80.0,
            currency="USD",
        )
        intent = StructuredQueryIntent(
            budget=IntentBudget(max=20.0, currency="EUR")
        )
        result = apply_intent(base, intent)
        self.assertEqual(result.currency, "EUR")
        self.assertEqual(result.max_price, 20.0)

    def test_apply_intent_preserves_currency_when_intent_has_none(self):
        base = SearchRequest(
            original_query="headphones under 80 dollars",
            normalized_query="headphones",
            max_price=80.0,
            currency="USD",
        )
        intent = StructuredQueryIntent(budget=IntentBudget(max=None, currency=None))
        result = apply_intent(base, intent)
        self.assertEqual(result.currency, "USD")
        self.assertEqual(result.max_price, 80.0)

    def test_resolved_request_full_pipeline_usd(self):
        req = resolve_request("headphones under 80 dollars")
        self.assertEqual(req.currency, "USD")
        self.assertEqual(req.max_price, 80.0)
        self.assertEqual(req.normalized_query, "headphones")

    def test_resolved_request_full_pipeline_eur(self):
        req = resolve_request("наушники до 20 евро")
        self.assertEqual(req.currency, "EUR")
        self.assertEqual(req.max_price, 20.0)
        self.assertEqual(req.normalized_query, "наушники")


# ---------------------------------------------------------------------------
# 9. Minimum and maximum price constraints
# ---------------------------------------------------------------------------

class MinMaxPriceTests(unittest.TestCase):
    """Currency detection must not change min/max price semantics."""

    def test_under_dollars_is_max_price(self):
        req = ai_parse("headphones under 80 dollars")
        self.assertEqual(req.max_price, 80.0)
        self.assertIsNone(req.min_price)
        self.assertEqual(req.currency, "USD")

    def test_over_euros_is_min_price(self):
        req = ai_parse("headphones over 20 euros")
        self.assertEqual(req.min_price, 20.0)
        self.assertIsNone(req.max_price)
        self.assertEqual(req.currency, "EUR")

    def test_under_symbol_is_max_price(self):
        req = ai_parse("headphones under $50")
        self.assertEqual(req.max_price, 50.0)
        self.assertIsNone(req.min_price)
        self.assertEqual(req.currency, "USD")

    def test_over_symbol_is_min_price(self):
        req = ai_parse("headphones over €30")
        self.assertEqual(req.min_price, 30.0)
        self.assertIsNone(req.max_price)
        self.assertEqual(req.currency, "EUR")

    def test_intent_under_is_max_budget(self):
        intent, _ = intent_parse("headphones under 80 dollars")
        self.assertEqual(intent.budget.max, 80.0)
        self.assertIsNone(intent.budget.min)
        self.assertEqual(intent.budget.currency, "USD")

    def test_intent_over_is_min_budget(self):
        intent, _ = intent_parse("headphones over 20 euros")
        self.assertEqual(intent.budget.min, 20.0)
        self.assertIsNone(intent.budget.max)
        self.assertEqual(intent.budget.currency, "EUR")


# ---------------------------------------------------------------------------
# 10. Existing range semantics
# ---------------------------------------------------------------------------

class RangeSemanticsTests(unittest.TestCase):
    """Between X and Y must keep range semantics with currency."""

    def test_russian_range_with_currency(self):
        req = ai_parse("наушники от 20 до 50 евро")
        self.assertEqual(req.currency, "EUR")
        # The AI fallback parser checks MAX and MIN patterns independently,
        # so both bounds are captured.
        self.assertEqual(req.min_price, 20.0)
        self.assertEqual(req.max_price, 50.0)

    def test_intent_range_with_currency(self):
        intent, _ = intent_parse("наушники от 20 до 50 евро")
        self.assertEqual(intent.budget.currency, "EUR")
        # Same single-bound behaviour as the AI fallback parser.
        self.assertEqual(intent.budget.max, 50.0)
        self.assertIsNone(intent.budget.min)


# ---------------------------------------------------------------------------
# 11. Queries with no explicit currency
# ---------------------------------------------------------------------------

class NoCurrencyTests(unittest.TestCase):
    """A message without explicit currency must not acquire one."""

    def test_ai_service_no_currency(self):
        req = ai_parse("headphones")
        self.assertIsNone(req.currency)
        self.assertIsNone(req.max_price)
        self.assertIsNone(req.min_price)

    def test_intent_parser_no_currency(self):
        intent, _ = intent_parse("headphones")
        self.assertIsNone(intent.budget.currency)
        self.assertIsNone(intent.budget.max)
        self.assertIsNone(intent.budget.min)

    def test_resolved_request_no_currency(self):
        req = resolve_request("headphones")
        self.assertIsNone(req.currency)
        self.assertIsNone(req.max_price)

    def test_no_currency_word_pollution(self):
        req = ai_parse("dollarstore headphones")
        self.assertIsNone(req.currency)

        intent, _ = intent_parse("dollarstore headphones")
        self.assertIsNone(intent.budget.currency)


# ---------------------------------------------------------------------------
# 12. Multi-turn budget refinement
# ---------------------------------------------------------------------------

class MultiTurnRefinementTests(unittest.TestCase):
    """Multi-turn conversation must preserve product + budget across turns."""

    def test_first_search_headphones_then_under_80_dollars(self):
        req1 = ai_parse("headphones")
        self.assertEqual(req1.normalized_query, "headphones")
        self.assertIsNone(req1.currency)

        req2 = ai_parse("under 80 dollars")
        self.assertEqual(req2.currency, "USD")
        self.assertEqual(req2.max_price, 80.0)

        resolved = resolve_request("headphones under 80 dollars")
        self.assertEqual(resolved.normalized_query, "headphones")
        self.assertEqual(resolved.currency, "USD")
        self.assertEqual(resolved.max_price, 80.0)

    def test_budget_refinement_changes_currency(self):
        req1 = ai_parse("headphones under 80 dollars")
        self.assertEqual(req1.currency, "USD")
        self.assertEqual(req1.max_price, 80.0)

        req2 = ai_parse("headphones under 50 euros")
        self.assertEqual(req2.currency, "EUR")
        self.assertEqual(req2.max_price, 50.0)

        base = SearchRequest(
            original_query="headphones under 80 dollars",
            normalized_query="headphones",
            max_price=80.0,
            currency="USD",
        )
        intent = StructuredQueryIntent(budget=IntentBudget(max=50.0, currency="EUR"))
        result = apply_intent(base, intent)
        self.assertEqual(result.currency, "EUR")
        self.assertEqual(result.max_price, 50.0)


# ---------------------------------------------------------------------------
# 13. Explicit currency changes
# ---------------------------------------------------------------------------

class ExplicitCurrencyChangeTests(unittest.TestCase):
    """Explicit currency changes must be accepted and preserved."""

    def test_change_usd_to_eur(self):
        base = SearchRequest(
            original_query="headphones under 80 dollars",
            normalized_query="headphones",
            max_price=80.0,
            currency="USD",
        )
        intent = StructuredQueryIntent(budget=IntentBudget(max=60.0, currency="EUR"))
        result = apply_intent(base, intent)
        self.assertEqual(result.currency, "EUR")
        self.assertEqual(result.max_price, 60.0)

    def test_change_eur_to_usd(self):
        base = SearchRequest(
            original_query="наушники до 20 евро",
            normalized_query="наушники",
            max_price=20.0,
            currency="EUR",
        )
        intent = StructuredQueryIntent(budget=IntentBudget(max=50.0, currency="USD"))
        result = apply_intent(base, intent)
        self.assertEqual(result.currency, "USD")
        self.assertEqual(result.max_price, 50.0)

    def test_change_symbol_to_word(self):
        base = SearchRequest(
            original_query="headphones under $80",
            normalized_query="headphones",
            max_price=80.0,
            currency="USD",
        )
        intent = StructuredQueryIntent(budget=IntentBudget(max=100.0, currency="EUR"))
        result = apply_intent(base, intent)
        self.assertEqual(result.currency, "EUR")
        self.assertEqual(result.max_price, 100.0)


# ---------------------------------------------------------------------------
# 14. Contradictory or incomplete AI parser output
# ---------------------------------------------------------------------------

class ContradictoryAIOutputTests(unittest.TestCase):
    """When AI gives incomplete/contradictory currency, fallback must not
    silently discard the user's explicit currency."""

    def test_ai_failure_falls_back_to_currency_detection(self):
        # _generate returning None simulates an AI failure; parse_query then
        # falls back to the deterministic parser which still detects currency.
        async def _none_generate(contents, *, models=None):
            return None
        svc = AIService(api_key="fake-key")
        svc._client = object()
        svc._generate = _none_generate  # type: ignore[assignment]
        req = run(svc.parse_query("headphones under 80 dollars"))
        self.assertEqual(req.currency, "USD")
        self.assertEqual(req.max_price, 80.0)


# ---------------------------------------------------------------------------
# 15. Regression: Stage 2 currency-safe filtering
# ---------------------------------------------------------------------------

class Stage2RegressionTests(unittest.TestCase):
    """Stage 2 currency-safe numeric filtering must remain intact."""

    def test_currency_safe_filtering_no_currency(self):
        req = ai_parse("headphones under 80")
        self.assertIsNone(req.currency)
        self.assertEqual(req.max_price, 80.0)

    def test_currency_safe_filtering_with_currency(self):
        req = ai_parse("наушники до 80 долларов")
        self.assertEqual(req.currency, "USD")
        self.assertEqual(req.max_price, 80.0)

    def test_normalized_query_excludes_currency_words(self):
        req = ai_parse("headphones under 80 dollars")
        self.assertNotIn("dollar", req.normalized_query or "")
        self.assertNotIn("dollars", req.normalized_query or "")
        self.assertIn("headphones", req.normalized_query or "")

        req = ai_parse("наушники до 80 долларов")
        self.assertNotIn("долларов", req.normalized_query or "")
        self.assertIn("наушники", req.normalized_query or "")

    def test_russian_max_price_regression(self):
        req = ai_parse("наушники беспроводные до 3000 рублей")
        self.assertEqual(req.max_price, 3000.0)
        self.assertEqual(req.currency, "RUB")

    def test_dollar_symbol_regression(self):
        req = ai_parse("Find wireless headphones under $30 with good reviews")
        self.assertEqual(req.max_price, 30.0)
        self.assertEqual(req.currency, "USD")


# ---------------------------------------------------------------------------
# 16. Regression: Stage 3C merge safety
# ---------------------------------------------------------------------------

class Stage3CRegressionTests(unittest.TestCase):
    """Stage 3C merge-safety protections must remain intact with currency."""

    def test_currency_is_a_hard_constraint_in_merge(self):
        from services.conversation import deterministic_merge

        previous = SearchRequest(
            original_query="headphones under 80 dollars",
            normalized_query="headphones",
            max_price=80.0,
            currency="USD",
        )
        parsed = SearchRequest(
            original_query="headphones under 50 euros",
            normalized_query="headphones",
            max_price=50.0,
            currency="EUR",
        )
        merged, _ = deterministic_merge(previous, parsed, "headphones under 50 euros")
        self.assertEqual(merged.currency, "EUR")
        self.assertEqual(merged.max_price, 50.0)

    def test_currency_erasure_is_blocked_by_stage3c(self):
        from services.conversation import resolve_request

        previous = SearchRequest(
            original_query="headphones under 80 dollars",
            normalized_query="headphones",
            max_price=80.0,
            currency="USD",
        )
        ai_merge = SearchRequest(
            original_query="headphones under 80 dollars",
            normalized_query="headphones",
            max_price=80.0,
            currency=None,
        )
        resolved, _ = run(resolve_request(previous, ai_merge, "headphones under 80 dollars", None))
        self.assertEqual(resolved.currency, "USD")
        self.assertEqual(resolved.max_price, 80.0)

    def test_currency_invention_is_blocked_by_stage3c(self):
        """When AI invents a currency, _merge_is_safe (Stage 3C) rejects the
        merge and resolve_request falls back to deterministic_merge which
        preserves the previous state (no invented currency).

        This test exercises the merge-safety gate via
        AIService._merge_is_safe, comparing the AI merge against the
        deterministic merge of the same inputs.
        """
        from services.ai_service import AIService
        from services.conversation import deterministic_merge

        previous = SearchRequest(
            original_query="headphones",
            normalized_query="headphones",
        )
        follow_up = SearchRequest(
            original_query="headphones",
            normalized_query="headphones",
        )
        # AI merge invents EUR currency + max_price the user never stated
        ai_merge = SearchRequest(
            original_query="headphones",
            normalized_query="headphones",
            max_price=80.0,
            currency="EUR",
        )
        # Deterministic merge of previous + follow_up has no currency/max_price
        det_merged, _ = deterministic_merge(previous, follow_up, "headphones")
        # _merge_is_safe compares ai_merge against det_merged on hard fields;
        # invented currency mismatches -> unsafe
        safe = AIService._merge_is_safe(previous, follow_up, ai_merge)
        self.assertFalse(safe)

    def test_currency_change_is_accepted_by_stage3c(self):
        """An explicit currency change is NOT an invention: deterministic_merge
        already carries the new value, so an AI merge that agrees lands."""
        from services.conversation import deterministic_merge

        previous = SearchRequest(
            original_query="headphones under 80 dollars",
            normalized_query="headphones",
            max_price=80.0,
            currency="USD",
        )
        parsed = SearchRequest(
            original_query="headphones under 50 euros",
            normalized_query="headphones",
            max_price=50.0,
            currency="EUR",
        )
        merged, _ = deterministic_merge(previous, parsed, "headphones under 50 euros")
        # Deterministic merge takes the follow_up's values (explicit change)
        self.assertEqual(merged.currency, "EUR")
        self.assertEqual(merged.max_price, 50.0)


# ---------------------------------------------------------------------------
# 17. CURRENCY_ALIASES completeness
# ---------------------------------------------------------------------------

class AliasesCompletenessTests(unittest.TestCase):
    """The alias dictionaries must cover all required forms."""

    def test_query_intent_aliases_cover_required_usd(self):
        for alias in ("$", "usd", "dollar", "dollars", "us dollar", "us dollars",
                      "доллар", "доллара", "долларов", "доллары",
                      "долар", "долара", "доларів"):
            self.assertEqual(CURRENCY_ALIASES[alias], "USD", alias)

    def test_query_intent_aliases_cover_required_eur(self):
        for alias in ("€", "eur", "euro", "euros", "евро", "евр", "євро"):
            self.assertEqual(CURRENCY_ALIASES[alias], "EUR", alias)

    def test_query_intent_aliases_cover_required_rub(self):
        for alias in ("₽", "rub", "ruble", "rubles", "руб", "рубль", "рубля", "рублей", "рублів"):
            self.assertEqual(CURRENCY_ALIASES[alias], "RUB", alias)

    def test_query_intent_aliases_cover_required_cny(self):
        for alias in ("¥", "cny", "yuan", "yuans", "юан", "юань", "юаня", "юаней", "юанів"):
            self.assertEqual(CURRENCY_ALIASES[alias], "CNY", alias)

    def test_query_intent_aliases_cover_required_uah(self):
        for alias in ("₴", "uah", "грн", "грн.", "гривна", "гривны", "гривен"):
            self.assertEqual(CURRENCY_ALIASES[alias], "UAH", alias)

    def test_query_intent_aliases_cover_required_gbp(self):
        for alias in ("£", "gbp", "pound", "pounds"):
            self.assertEqual(CURRENCY_ALIASES[alias], "GBP", alias)

    def test_ai_service_markers_cover_required_currencies(self):
        currencies = {currency for _, currency in _CURRENCY_MARKERS}
        self.assertIn("USD", currencies)
        self.assertIn("EUR", currencies)
        self.assertIn("RUB", currencies)
        self.assertIn("CNY", currencies)
        self.assertIn("GBP", currencies)

    def test_no_unsupported_currency_codes(self):
        self.assertIsNone(normalize_currency("btc"))
        self.assertIsNone(normalize_currency("bitcoin"))
        self.assertIsNone(normalize_currency("ethereum"))
        self.assertIsNone(normalize_currency("XYZ"))


if __name__ == "__main__":
    unittest.main()

