# -*- coding: utf-8 -*-
"""G25.2-D Stage 35: generic-container product-type guard.

The Stage-33 real failure - query «Набор для рыбака» answered first with a
fishing-THEMED party set (plates, napkins, cups) - is a product-role bug: the
generic container noun «набор» names the FORM of the offer, not the product
inside it, yet it alone awarded an EXACT (tier-2) verdict to any title that
merely led with «Набор».

This module pins the deterministic correction:

* a generic container noun (RU «набор»/«комплект», EN «set»/«kit») alone can
  never make an unrelated themed/decorative set an EXACT match;
* a container query that DOES name a concrete product («набор инструментов»,
  «удочка для рыбалки») keeps its EXACT verdict for a genuine set;
* a bare-container query («набор инструментов») still returns real sets;
* the rule works for photo-search-shaped SEARCH requests that carry NO
  structured ``intent_*`` fields (it reads the request's own query nouns);
* non-SEARCH modes are untouched (``classify_product`` returns ``unverified``).

Stage 39 refinement (supersedes the Stage-35 title-position heuristic):
the Stage-35 rule asked whether the TITLE named the requested use in a
"genuine role", which the real Stage-36 provider pool defeated - the
audience word «рыбака» appears in ordinary keyword-stuffed tail positions
("… подарок для рыбака"), so every themed set still matched. Stage 37 then
proved a genuine set («Набор рыболовных снастей…») and a themed one
(«Набор посуды… на тему рыбалки») are STRUCTURALLY IDENTICAL under a
container + audience-only query - separating them needs an
activity→instrument lexicon this project does not build. So for a
container + audience-only request (product_type is a generic container,
the only other concept is an audience in ``intent_compatibility``, and NO
concrete product noun is named) the guard now withholds EXACT for EVERY
container-led candidate - genuine or themed - keeping them as related
(tier-1) results that are never rejected. This corrects the misleading
verdict but does NOT reorder (ranking would need a lexicon). Queries that
DO name a concrete product are untouched and keep the Stage-35 behaviour.

Everything is offline: ``AIService(api_key=None)`` runs the local fallback
parser and every product is a plain :class:`Product`. No AliExpress, no
Gemini. Fixtures are realistic RU listings of the same shape as the recorded
provider rows; the exact «Набор для рыбака» provider pool was never captured,
so no provider response is invented here.
"""

from __future__ import annotations

import unittest

from models.product import Product
from models.search_request import SearchMode, SearchRequest
from services.ai_service import AIService
from services.comparison_service import sort_products
from services.query_intent import apply_intent, fallback_intent
from services.relevance import (
    _has_structured_intent,
    _is_generic_container,
    classify_product,
    filter_relevant,
    relevance_tier,
)

from tests.test_search_intent import make_listing


def ranked_listing(product_id: str, title: str, orders_count: int) -> Product:
    """A listing with a real orders count, for popularity tie-break checks."""
    return make_listing(product_id, title).model_copy(
        update={"orders_count": orders_count}
    )


def request_for(query: str) -> SearchRequest:
    """The intent-bearing production request for one sentence (local parser)."""
    return apply_intent(AIService._fallback_parse(query), fallback_intent(query))


def photo_request(query: str) -> SearchRequest:
    """The request SHAPE photo search builds: mode=SEARCH, NO intent fields."""
    return SearchRequest(
        original_query=query,
        normalized_query=query,
        mode=SearchMode.SEARCH,
    )


FISHING_QUERY = "Набор для рыбака"

# Stage-33 quartet (the four products the user actually saw).
PARTY_SET = (
    "Набор бумажных тарелок и салфеток с рыбами, одноразовые стаканы, "
    "декор для вечеринки на тему рыбалки"
)
PUZZLE = "3D деревянный сборный пазл-модель рыбацкая лодка, конструктор для сборки"
AQUARIUM_DECOR = (
    "Декорация для аквариума и пруда с миниатюрной рыболовной лодкой, "
    "ландшафтный декор"
)
FISHING_TOOLS = (
    "Набор рыболовных снастей и инструментов для рыбалки: крючки, катушка, "
    "плоскогубцы"
)


class GenericContainerRecognition(unittest.TestCase):
    """The closed container class - RU + EN, and what it must NOT swallow."""

    def test_russian_container_forms(self) -> None:
        for word in ("набор", "набора", "наборы", "наборов", "комплект",
                     "комплекты", "комплекта"):
            self.assertTrue(_is_generic_container(word), word)

    def test_english_container_forms(self) -> None:
        for word in ("set", "sets", "kit", "kits"):
            self.assertTrue(_is_generic_container(word), word)

    def test_ordinary_words_are_not_containers(self) -> None:
        for word in ("kitchen", "settle", "настройка", "инструментов",
                     "рыболовных", "камера"):
            self.assertFalse(_is_generic_container(word), word)


class Stage33FishingRegression(unittest.TestCase):
    """Acceptance A: the genuine set outranks the unrelated themed sets."""

    def setUp(self) -> None:
        self.request = request_for(FISHING_QUERY)

    def test_genuine_fishing_set_is_related_not_false_exact(self) -> None:
        # Stage 39: «Набор для рыбака» is a container + audience-only request
        # (product_type=«набор», compatibility=[«рыбака»], NO concrete product
        # noun). The set's contents are not stated, and Stage 37 proved a
        # genuine set and a themed one are structurally identical - telling
        # them apart needs an activity→instrument lexicon this project does
        # not build. So the conservative guard keeps EVERY container-led
        # candidate as a related (tier-1) result and withholds EXACT; the
        # genuine set is still KEPT (never rejected), just not falsely exact.
        verdict = classify_product(
            make_listing("tools", FISHING_TOOLS), self.request
        )
        self.assertEqual(verdict, "mentioned")
        self.assertLess(
            relevance_tier(make_listing("tools", FISHING_TOOLS), self.request), 2
        )
        kept, _ = filter_relevant(
            [make_listing("tools", FISHING_TOOLS)], self.request
        )
        self.assertEqual([p.product_id for p in kept], ["tools"])

    def test_themed_party_set_is_not_exact(self) -> None:
        verdict = classify_product(make_listing("party", PARTY_SET), self.request)
        self.assertNotEqual(verdict, "exact")
        self.assertLess(
            relevance_tier(make_listing("party", PARTY_SET), self.request), 2
        )

    def test_themed_puzzle_is_not_exact(self) -> None:
        self.assertNotEqual(
            classify_product(make_listing("puzzle", PUZZLE), self.request), "exact"
        )

    def test_no_container_led_candidate_is_false_exact(self) -> None:
        # Stage 39: under a container + audience-only query NO container-led
        # candidate - genuine OR themed - earns a false EXACT, regardless of
        # popularity. The guard corrects the misleading verdict; it does NOT
        # reorder (Stage 37: ranking needs a lexicon, which is out of scope).
        pool = [
            ranked_listing("party", PARTY_SET, 5000),
            make_listing("puzzle", PUZZLE),
            ranked_listing("tools", FISHING_TOOLS, 50),
        ]
        for product in pool:
            verdict = classify_product(product, self.request)
            self.assertNotEqual(verdict, "exact", product.title)
            self.assertLess(relevance_tier(product, self.request), 2)
        # Nothing is dropped: every container-led candidate stays a usable
        # related result.
        kept, _ = filter_relevant(list(pool), self.request)
        self.assertEqual(
            sorted(p.product_id for p in kept), ["party", "puzzle", "tools"]
        )

    def test_themed_sets_survive_as_related_not_rejected(self) -> None:
        # A themed set is still useful; it must not be silently dropped.
        kept, _ = filter_relevant(
            [make_listing("party", PARTY_SET)], self.request
        )
        self.assertEqual([p.product_id for p in kept], ["party"])


class LegitimateConcreteSets(unittest.TestCase):
    """Acceptance B: a genuine set of the requested product stays EXACT."""

    def test_russian_tools_set_stays_exact(self) -> None:
        request = request_for("набор инструментов")
        self.assertEqual(
            classify_product(
                make_listing("t", "Набор инструментов 100 в 1, отвертки, биты"),
                request,
            ),
            "exact",
        )

    def test_english_set_query_keeps_exact_for_a_real_set(self) -> None:
        request = request_for("fishing tackle set")
        self.assertEqual(
            classify_product(
                make_listing("t", "Fishing tackle set, hooks, reels, pliers"),
                request,
            ),
            "exact",
        )

    def test_container_led_but_themed_set_is_downgraded(self) -> None:
        request = request_for("набор инструментов")
        verdict = classify_product(
            make_listing("d", "Набор наклеек на тему инструментов, декор"),
            request,
        )
        self.assertNotEqual(verdict, "exact")


class BareContainerQueries(unittest.TestCase):
    """Acceptance C: a bare «набор X» query still returns real sets."""

    def test_bare_container_query_still_matches_a_set(self) -> None:
        request = request_for("набор инструментов")
        self.assertEqual(
            classify_product(
                make_listing("s", "Набор инструментов для ремонта, 46 предметов"),
                request,
            ),
            "exact",
        )

    def test_bare_container_without_use_preserves_behaviour(self) -> None:
        # A query that is ONLY the container has nothing more specific to
        # bind, so the guard must not fire and today's verdict stands.
        request = request_for("набор")
        self.assertEqual(
            classify_product(
                make_listing("s", "Набор подарочный ассорти, универсальный"),
                request,
            ),
            "exact",
        )


class CrossCategoryGeneralization(unittest.TestCase):
    """Acceptance D: the rule is category-independent (no keyword lists)."""

    def _pair(self, query: str, genuine: str, themed: str) -> None:
        # Stage 39: «Набор для X» is a container + audience-only request, so
        # NEITHER a genuine container-led set NOR a themed one earns EXACT -
        # they are structurally identical without an activity→instrument
        # lexicon (Stage 37). The category-independent guarantee is that the
        # guard fires the SAME way for every activity with no keyword list:
        # both stay related (tier-1) and both are kept, never rejected.
        request = request_for(query)
        for pid, title in (("g", genuine), ("d", themed)):
            with self.subTest(title=title):
                self.assertNotEqual(
                    classify_product(make_listing(pid, title), request),
                    "exact",
                    title,
                )
                self.assertLess(
                    relevance_tier(make_listing(pid, title), request), 2
                )
        kept, _ = filter_relevant(
            [make_listing("g", genuine), make_listing("d", themed)], request
        )
        kept_ids = {p.product_id for p in kept}
        # The genuine candidate must never be over-rejected by the guard; it
        # stays a usable related result. (The themed candidate's survival is
        # a separate R3 word-overlap question, not asserted here.)
        self.assertIn("g", kept_ids)

    def test_beauty_makeup(self) -> None:
        self._pair(
            "Набор для макияжа",
            "Набор кистей для макияжа 32 шт, спонжи",
            "Набор наклеек на тему макияжа, украшение для телефона",
        )

    def test_automotive_car_care(self) -> None:
        self._pair(
            "Набор для мойки машины",
            "Набор для мойки машины: щетка, шланг, салфетка из микрофибры",
            "Набор наклеек на тему авто, декор для салона",
        )

    def test_childrens_creativity(self) -> None:
        self._pair(
            "Набор для творчества",
            "Набор для творчества: пластилин, формочки, коврик",
            "Набор тарелок на тему детского праздника, декор",
        )

    def test_kitchen_baking(self) -> None:
        self._pair(
            "Набор для выпечки",
            "Набор для выпечки: формы, лопатки, кондитерский мешок",
            "Набор приколовых салфеток на тему кухни, декор",
        )

    def test_tools_repair(self) -> None:
        self._pair(
            "Набор для ремонта",
            "Набор для ремонта велосипеда: ключи, мультитул",
            "Набор стикеров на тему ремонта, декор для гаража",
        )


class PhotoSearchPath(unittest.TestCase):
    """Acceptance E: SEARCH-shaped request with NO structured intent fields."""

    def test_photo_request_genuine_set_is_exact(self) -> None:
        request = photo_request("набор для рыбака")
        self.assertEqual(
            classify_product(make_listing("tools", FISHING_TOOLS), request),
            "exact",
        )

    def test_photo_request_themed_set_is_not_exact(self) -> None:
        request = photo_request("набор для рыбака")
        self.assertNotEqual(
            classify_product(make_listing("party", PARTY_SET), request), "exact"
        )

    def test_photo_genuine_outranks_themed(self) -> None:
        request = photo_request("набор для рыбака")
        pool = [
            ranked_listing("party", PARTY_SET, 9000),
            ranked_listing("tools", FISHING_TOOLS, 30),
        ]
        ordered = [p.product_id for p in sort_products(list(pool), request)]
        self.assertEqual(ordered[0], "tools")


class ModeIsolation(unittest.TestCase):
    """Acceptance F: non-SEARCH modes keep their existing verdicts."""

    def test_non_search_modes_stay_unverified(self) -> None:
        request = request_for(FISHING_QUERY)
        for mode in (SearchMode.CATEGORY, SearchMode.DISCOVERY,
                     SearchMode.FEED, SearchMode.IMAGE, SearchMode.POPULAR):
            with self.subTest(mode=mode):
                request.mode = mode
                for title in (FISHING_TOOLS, PARTY_SET, PUZZLE, AQUARIUM_DECOR):
                    self.assertEqual(
                        classify_product(make_listing("p", title), request),
                        "unverified",
                    )


class Stage39ContainerAudienceGuard(unittest.TestCase):
    """Stage 39: container + audience-only request withholds EXACT.

    The Stage-36 live A/B showed the Stage-35 title-position guard changed
    zero candidates because «рыбака» appears in ordinary keyword-stuffed
    tail positions. Stage 37 proved genuine and themed container-led sets
    are structurally identical without an activity→instrument lexicon. The
    Stage-39 correction keys on the REQUEST shape (existing intent fields):
    a generic-container product_type + an audience in compatibility + NO
    concrete product noun ⇒ no container-led candidate may be EXACT.
    """

    def test_party_set_mentions_audience_in_tail_is_not_exact(self) -> None:
        # Case 1: a container-led party-supply title cannot be exact merely
        # because it mentions the audience/activity.
        request = request_for(FISHING_QUERY)
        verdict = classify_product(make_listing("party", PARTY_SET), request)
        self.assertEqual(verdict, "mentioned")
        self.assertLess(
            relevance_tier(make_listing("party", PARTY_SET), request), 2
        )

    def test_decoration_set_is_not_exact(self) -> None:
        # Case 1: a container-led decoration title likewise stays related.
        request = request_for(FISHING_QUERY)
        verdict = classify_product(make_listing("decor", AQUARIUM_DECOR), request)
        self.assertNotEqual(verdict, "exact")
        self.assertLess(
            relevance_tier(make_listing("decor", AQUARIUM_DECOR), request), 2
        )

    def test_keyword_stuffed_audience_in_tail_does_not_bypass(self) -> None:
        # Case 2: «рыбака» in an ordinary tail position («… подарок для
        # рыбака») must not let the candidate back into EXACT - the guard is
        # a REQUEST-shape test, independent of where the word sits.
        request = request_for(FISHING_QUERY)
        stuffed = (
            "Набор бумажных тарелок, стаканы и скатерть на тему моря, "
            "отличный подарок для рыбака, украшение для вечеринки"
        )
        self.assertNotEqual(
            classify_product(make_listing("s", stuffed), request), "exact"
        )

    def test_bare_container_plus_concrete_noun_keeps_exact(self) -> None:
        # Case 3: «набор инструментов» names a concrete product, so a genuine
        # tool set stays EXACT (tier 2) - the guard must not fire.
        request = request_for("набор инструментов")
        title = "Набор инструментов для ремонта, 108 предметов, отвертки, биты"
        self.assertEqual(classify_product(make_listing("t", title), request), "exact")
        self.assertEqual(relevance_tier(make_listing("t", title), request), 2)

    def test_concrete_product_query_keeps_exact(self) -> None:
        # Case 4: «удочка для рыбалки» names the concrete product; a genuine
        # rod keeps its existing EXACT classification untouched.
        request = request_for("удочка для рыбалки")
        title = "Удочка карбоновая телескопическая для рыбалки, 3.6 м"
        self.assertEqual(classify_product(make_listing("r", title), request), "exact")
        self.assertEqual(relevance_tier(make_listing("r", title), request), 2)

    def test_non_container_title_mentioning_audience_is_not_guarded(self) -> None:
        # Case 5: the guard must not touch a title that does NOT lead with a
        # container - «Рыболовный календарь…» is judged by the ordinary path,
        # not demoted by the container rule.
        request = request_for(FISHING_QUERY)
        title = "Рыболовный календарь обратного отсчета для рыбака"
        verdict = classify_product(make_listing("cal", title), request)
        self.assertNotEqual(verdict, "exact")

    def test_photo_shaped_request_without_intent_is_unchanged(self) -> None:
        # Case 6: a SEARCH request with NO structured intent_* fields keeps
        # its existing behaviour - the Stage-39 guard is intent-gated and is
        # inert here (documented limitation, no intent invented for photo).
        request = photo_request("набор для рыбака")
        self.assertFalse(_has_structured_intent(request))
        verdict = classify_product(make_listing("tools", FISHING_TOOLS), request)
        self.assertIn(verdict, {"exact", "mentioned"})

    def test_non_search_modes_stay_unverified(self) -> None:
        # Case 7: non-SEARCH modes are untouched by the new guard.
        request = request_for(FISHING_QUERY)
        for mode in (SearchMode.CATEGORY, SearchMode.DISCOVERY,
                     SearchMode.FEED, SearchMode.IMAGE, SearchMode.POPULAR):
            with self.subTest(mode=mode):
                request.mode = mode
                for title in (FISHING_TOOLS, PARTY_SET, AQUARIUM_DECOR):
                    self.assertEqual(
                        classify_product(make_listing("p", title), request),
                        "unverified",
                    )

    def test_english_fallback_does_not_split_for_limitation_documented(
        self,
    ) -> None:
        # Case 8: the deterministic fallback does NOT split English "for X",
        # so "fishing kit for fisherman" keeps the WHOLE phrase as
        # product_type. The Stage-39 guard therefore does not fire and English
        # parity is NOT claimed - this documents the limitation without
        # changing the parser.
        request = request_for("fishing kit for fisherman")
        self.assertEqual(request.intent_compatibility, [])
        self.assertIn("kit", (request.intent_product_type or "").casefold())


if __name__ == "__main__":
    unittest.main()
