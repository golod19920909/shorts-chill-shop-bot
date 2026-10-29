# -*- coding: utf-8 -*-
"""G25.2-D Stage 43: short-token prefix false-match guard.

Deterministic bug found while auditing the more specific query
«Набор рыболовных снастей»: the Russian preposition «с» is a single
letter, so under the old ``nouns_match`` prefix rule it prefix-matched the
product word «снастей» (one starts with the other). That let a themed
party-supply title («тарелка с принтом рыбы») count the query term
«снастей» as product evidence and receive a false ``exact`` verdict.

The fix in :func:`services.relevance.nouns_match` treats any word shorter
than :data:`~services.relevance.MIN_TERM_LENGTH` as a grammatical token,
not a product noun: it can only equal ITSELF and never prefix-matches a
longer product word. Full-word matches, and inflections of words long
enough to carry a product meaning, are unchanged.

Scope: matching logic only. No schema, parsing, ranking, curation, or
non-SEARCH pipeline is touched, and no fishing dictionary is introduced.
Everything is offline (local fallback parser + plain :class:`Product`).
"""

from __future__ import annotations

import unittest

from models.search_request import SearchMode
from services.relevance import (
    MIN_TERM_LENGTH,
    nouns_match,
)

from tests.test_stage35_generic_container import (
    AQUARIUM_DECOR,
    FISHING_QUERY,
    FISHING_TOOLS,
    PARTY_SET,
    classify_product,
    make_listing,
    photo_request,
    request_for,
    relevance_tier,
)


class ShortTokenPrepositionGuard(unittest.TestCase):
    """The «с» -> «снастей» false match must never happen."""

    def test_min_term_length_is_three(self) -> None:
        self.assertEqual(MIN_TERM_LENGTH, 3)

    def test_single_letter_preposition_does_not_match_product_word(self) -> None:
        # Acceptance 1: «с» must not match «снастей» as product evidence.
        self.assertFalse(nouns_match("с", "снастей"))
        self.assertFalse(nouns_match("снастей", "с"))
        # Identity is still preserved.
        self.assertTrue(nouns_match("с", "с"))

    def test_other_short_grammatical_tokens_are_guarded(self) -> None:
        # General, not special-cased to «с»: any token shorter than
        # MIN_TERM_LENGTH can only equal itself.
        for token in ("в", "к", "у", "по", "до"):
            self.assertFalse(nouns_match(token, "подарок"), token)
            self.assertTrue(nouns_match(token, token), token)


class LegitimateMatchesPreserved(unittest.TestCase):
    """Real product nouns must keep matching as before."""

    def test_long_inflections_still_match_by_prefix(self) -> None:
        # Acceptance 5: the existing 4-letter prefix rule for long words.
        self.assertTrue(nouns_match("снастей", "снасти"))
        self.assertTrue(nouns_match("удочка", "удочки"))
        self.assertTrue(nouns_match("рыболовных", "рыболова"))
        self.assertTrue(nouns_match("плоскогубцы", "плоскогубцев"))

    def test_medium_words_match_by_prefix_relation(self) -> None:
        # Words that reach MIN_TERM_LENGTH but not PREFIX_MATCH_LENGTH still
        # compare by the prefix-relation branch (one contains the other).
        self.assertTrue(nouns_match("рыба", "рыбак"))
        self.assertTrue(nouns_match("чай", "чайник"))


class SpecificQueryClassification(unittest.TestCase):
    """Query «Набор рыболовных снастей»: genuine gear is EXACT, themed is not."""

    QUERY = "Набор рыболовных снастей"

    def setUp(self) -> None:
        self.request = request_for(self.QUERY)

    def test_genuine_fishing_gear_is_exact(self) -> None:
        # Acceptance 2: real fishing-tackle titles still classify EXACT/2.
        for title in (
            "Набор рыболовных снастей и инструментов: крючки, катушка, "
            "плоскогубцы",
            "Набор рыболовных приманок, блесны и крючков для рыбалки",
        ):
            with self.subTest(title=title[:30]):
                self.assertEqual(
                    classify_product(make_listing("gear", title), self.request),
                    "exact",
                )
                self.assertEqual(
                    relevance_tier(make_listing("gear", title), self.request), 2
                )

    def test_real_party_title_from_stage40_pool_flips_with_fix(self) -> None:
        # Acceptance 3, non-vacuous: this is the EXACT party-supply title
        # captured live in the Stage-40 pool for «набор рыбака». Under the
        # pre-fix ``nouns_match`` the standalone preposition «с» (…тарелка
        # *с* принтом…) prefix-matched «снастей» and produced a false
        # ``exact``; the short-token guard removes that single link, so the
        # title - which names no «снастей» at all - can no longer be EXACT.
        # Verified against the pre-fix logic: verdict was ``exact`` before,
        # and is ``mentioned`` now (this test genuinely exercises the fix).
        party = (
            "Набор одноразовой посуды для рыбалки, бумажная тарелка с принтом "
            "рыбы, чашка, скатерть, баннер, декор для дня рождения на тему "
            "маленького рыбака"
        )
        verdict = classify_product(make_listing("party", party), self.request)
        self.assertNotEqual(verdict, "exact")
        self.assertLess(
            relevance_tier(make_listing("party", party), self.request), 2
        )

    def test_aquarium_decor_remains_not_exact(self) -> None:
        # Acceptance 4: pond/aquarium decoration is not a «снастей» product.
        verdict = classify_product(make_listing("decor", AQUARIUM_DECOR), self.request)
        self.assertNotEqual(verdict, "exact")
        self.assertLess(
            relevance_tier(make_listing("decor", AQUARIUM_DECOR), self.request), 2
        )


class Stage39GuardUnaffected(unittest.TestCase):
    """Acceptance 6: the ambiguous-container guard still holds unchanged."""

    def test_ambiguous_container_still_withholds_exact(self) -> None:
        # «Набор для рыбака» is container + audience-only: neither the party
        # set nor the genuine set may be EXACT - behaviour identical to
        # Stage 39 (the short-token fix does not reopen this).
        request = request_for(FISHING_QUERY)
        for label, title in (("party", PARTY_SET), ("tools", FISHING_TOOLS)):
            with self.subTest(label=label):
                self.assertNotEqual(
                    classify_product(make_listing(label, title), request), "exact"
                )
                self.assertLess(
                    relevance_tier(make_listing(label, title), request), 2
                )


class NonSearchAndPhotoUnaffected(unittest.TestCase):
    """Acceptance 7: non-SEARCH modes and the photo path are unchanged."""

    def test_non_search_modes_stay_unverified(self) -> None:
        request = request_for("Набор рыболовных снастей")
        for mode in (SearchMode.CATEGORY, SearchMode.DISCOVERY,
                     SearchMode.FEED, SearchMode.IMAGE, SearchMode.POPULAR):
            with self.subTest(mode=mode):
                request.mode = mode
                for title in (FISHING_TOOLS, PARTY_SET):
                    self.assertEqual(
                        classify_product(make_listing("p", title), request),
                        "unverified",
                    )

    def test_photo_shaped_request_still_verified(self) -> None:
        # Photo search (mode=SEARCH, no intent_*) is unaffected by the fix:
        # it classifies through the ordinary path exactly as before.
        request = photo_request("Набор рыболовных снастей")
        verdict = classify_product(make_listing("gear", FISHING_TOOLS), request)
        self.assertIn(verdict, {"exact", "mentioned", "noun_miss"})


if __name__ == "__main__":
    unittest.main()

