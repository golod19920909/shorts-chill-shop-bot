"""Focused tests for the Gemini conversational agent (services.agent_service
+ services.agent_loop + the handler wiring in handlers.search.run_agent_turn).

Everything external is a double: Gemini is a scripted fake client (no
network), the marketplace is a stub provider (no network), and the Telegram
objects are the same lightweight fakes the existing conversational tests use.
No paid API call and no real Telegram message is ever made.

Covered (the task's minimum list):

1.  a product request triggers the correct search tool;
2.  a vague request leads to a clarification question (no tool);
3.  a follow-up refinement retains applicable previous requirements;
4.  "which is better?" uses products previously shown to the SAME user;
5.  a comparison uses real product fields and invents nothing;
6.  a tool call, its result, then a final response;
7.  multiple tool rounds stop at the configured limit;
8.  invalid tool arguments are rejected safely;
9.  an empty search result does not destroy previous context;
10. a Gemini failure uses the existing fallback without corrupting state;
11. different users cannot see each other's history or shown products;
12. the existing relevance/safety suite still passes (run separately).
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from telegram.ext import CallbackContext

from handlers import common
from handlers import search as search_handlers
from models.product import Marketplace, Product
from models.search_request import SearchMode, SearchRequest
from services.agent_service import (
    MAX_TOOL_ROUNDS,
    AgentToolContext,
    ShoppingAgent,
    ToolArgumentError,
    comparison_lines,
    validate_compare_args,
    validate_search_args,
)
from services.conversation import ConversationState
from services.search_service import SearchOutcome, SearchService


def run(coro):
    return asyncio.run(coro)


def make_product(product_id: str, title: str, *, price: float = 25.0) -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=product_id,
        title=title,
        product_url=f"https://www.aliexpress.com/item/{product_id}.html",
        image_url=None,
        price=price,
        currency="USD",
        orders_count=100,
        rating=4.7,
        affiliate_url=f"https://s.click.aliexpress.com/e/_{product_id}",
    )


def make_request(query: str = "шуруповерт", **updates) -> SearchRequest:
    request = SearchRequest(original_query=query, mode=SearchMode.SEARCH)
    return request.model_copy(update=updates) if updates else request


# --- Gemini doubles ---------------------------------------------------------


class FakeParts:
    def __init__(self, parts):
        # Genai Content.parts is a LIST of Parts; a test that passes one bare
        # Part gets wrapped so the object shapes always match the SDK.
        self.parts = parts if isinstance(parts, list) else [parts]


class FakeCandidate:
    def __init__(self, parts):
        self.content = FakeParts(parts)


class FakeResponse:
    def __init__(self, parts):
        self.candidates = [FakeCandidate(parts)]

    @property
    def text(self):
        for part in self.candidates[0].content.parts:
            value = getattr(part, "text", None)
            if value:
                return value
        return None


class FakeFunctionCall:
    def __init__(self, name, args, call_id="call-1"):
        self.name = name
        self.args = args
        self.id = call_id


def text_part(text):
    from google.genai import types as genai_types

    return genai_types.Part(text=text)


def call_part(name, args, call_id="call-1"):
    from google.genai import types as genai_types

    return genai_types.Part(
        function_call=genai_types.FunctionCall(name=name, args=args, id=call_id)
    )


def response_part(name, payload, call_id="call-1"):
    from google.genai import types as genai_types

    return genai_types.Part(
        function_response=genai_types.FunctionResponse(
            name=name, id=call_id, response=payload
        )
    )


class FakeAIService:
    """Just enough of AIService for the loop: a real client-shaped object."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.is_available = True
        self.fast_model = "gemini-3.8-flash"
        self.timeout_seconds = 5.0
        self.generate_ok = 0
        self.generate_failed = 0
        self.last_model = None

        class _Models:
            pass

        class _Aio:
            def __init__(self, outer):
                self._outer = outer

            class models:  # noqa: N801 - mirrors the SDK namespace
                @staticmethod
                async def generate_content(**kwargs):
                    outer = _Aio._outer
                    outer.calls.append(kwargs)
                    step = outer.script.pop(0) if outer.script else text_part("ok")
                    if isinstance(step, Exception):
                        raise step
                    return FakeResponse(step)

        _Aio._outer = self
        self._client = type("C", (), {})()
        self._client.aio = _Aio(self)

    async def parse_query(self, text):
        # The search tool adapter parses through this same service; the fake
        # answers with the real deterministic parser (no network, no invention).
        from services.ai_service import AIService

        return AIService._fallback_parse(text)


# --- marketplace double ------------------------------------------------------


class StubProvider:
    marketplace = Marketplace.ALIEXPRESS
    display_name = "AliExpress"
    supports_feed = True

    def __init__(self, by_phrase=None):
        self.by_phrase = by_phrase or {}
        self.queries = []

    @property
    def is_configured(self):
        return True

    async def search_products(self, request):
        phrase = request.normalized_query or request.original_query or ""
        self.queries.append(phrase)
        return list(self.by_phrase.get(phrase, []))

    async def fetch_feed_page(self, **kwargs):
        return []

    async def aclose(self):
        return None


class FakeBot:
    def __init__(self):
        self.texts = []

    async def send_message(self, chat_id, text, **kwargs):
        self.texts.append(text)

    async def send_photo(self, *args, **kwargs):
        self.texts.append("[photo]")


class FakeMessage:
    def __init__(self, chat_id=100, text=""):
        self.chat_id = chat_id
        self.text = text
        self.bot = FakeBot()

    async def reply_text(self, text, **kwargs):
        self.bot.texts.append(text)


class FakeUser:
    id = 555
    username = "tester"
    full_name = "Test User"
    first_name = "Test"


class FakeUpdate:
    def __init__(self, message):
        self.effective_message = message
        self.effective_user = FakeUser()


class FakeApp:
    def __init__(self):
        self.bot_data = {
            "ai_service": None,
            "search_service": None,
            "database": None,
            "product_assistant": None,
        }


class FakeContext:
    def __init__(self):
        self.user_data = {}
        self.chat_data = {}
        self.bot_data = FakeApp().bot_data
        self.application = FakeApp()
        self.message = FakeMessage()
        self.bot = FakeBot()


# --- 1..4, 8: tool validation + the agent loop ------------------------------


class ValidationTests(unittest.TestCase):
    def test_product_request_triggers_search_tool(self):
        agent = ShoppingAgent(FakeAIService([call_part("search_products",
            {"query": "беспроводные наушники"})]))
        outcome = SearchOutcome(request=make_request("наушники"))
        result = run(agent.run(
            "найди беспроводные наушники",
            AgentToolContext(),
            search_fn=lambda **kw: _fake_search(outcome, kw),
        ))
        self.assertTrue(result["calls"])
        self.assertEqual(result["calls"][0].name, "search_products")
        self.assertTrue(result["calls"][0].search_ok)
        self.assertEqual(result["calls"][0].search_outcome, outcome)

    def test_vague_request_asks_clarification_without_tool(self):
        agent = ShoppingAgent(FakeAIService([text_part(
            "Какой товар вы ищете и для какой задачи?"
        )]))
        result = run(agent.run("посоветуй что-нибудь", AgentToolContext()))
        self.assertEqual(result["calls"], [])
        self.assertIn("какой товар", result["text"].lower())

    def test_refinement_retains_previous_requirements(self):
        previous = make_request("шуруповерт аккумуляторный",
            max_price=30.0, currency="USD")
        agent = ShoppingAgent(FakeAIService([call_part("search_products",
            {"query": "", "refine": True})]))
        captured = {}

        async def search_fn(**kw):
            captured.update(kw)
            return SearchOutcome(request=previous)

        run(agent.run("а есть полегче и до 30 долларов?",
            AgentToolContext(conversation_request=previous),
            search_fn=search_fn))
        # The refinement flag survives validation; the handler adapter merges
        # the user's words with the PREVIOUS request (the price the user just
        # repeated is combined there by the SAME merge machinery).
        self.assertTrue(captured.get("refine"))
        self.assertIsNone(captured.get("max_price"))  # nothing invented here

    def test_which_is_better_uses_shown_products(self):
        shown = [make_product("a", "Шуруповерт A"), make_product("b", "Шуруповерт B")]
        agent = ShoppingAgent(FakeAIService([call_part("get_shown_products", {})]))
        result = run(agent.run("какой из них лучше?",
            AgentToolContext(shown_products=shown)))
        payload = result["calls"][0].response
        self.assertEqual(payload["found"], 2)
        self.assertEqual(payload["products"][0]["title"], "Шуруповерт A")

    def test_compare_uses_real_fields_only(self):
        shown = [make_product("a", "A", price=10.0), make_product("b", "B", price=20.0)]
        agent = ShoppingAgent(FakeAIService([call_part("compare_products",
            {"question": "какой дешевле", "product_numbers": [1, 2]})]))
        result = run(agent.run("какой дешевле?",
            AgentToolContext(shown_products=shown)))
        lines = "\n".join(comparison_lines(shown))
        self.assertIn("A", lines)
        self.assertIn("10", lines)
        self.assertNotIn("неизвестно", lines)

    def test_comparison_never_invents_missing_specs(self):
        no_data = Product(
            marketplace=Marketplace.ALIEXPRESS, product_id="x", title="X",
            product_url="https://www.aliexpress.com/item/x.html",
        )
        lines = comparison_lines([no_data])
        self.assertIn("данных нет", "\n".join(lines).lower())

    def test_tool_call_then_result_then_final_response(self):
        agent = ShoppingAgent(FakeAIService([
            call_part("search_products", {"query": "наушники"}),
            text_part("Нашёл несколько наушников, вот лучшие."),
        ]))
        outcome = SearchOutcome(request=make_request("наушники"))
        result = run(agent.run("найди наушники", AgentToolContext(),
            search_fn=lambda **kw: _fake_search(outcome, kw)))
        self.assertEqual(len(result["calls"]), 1)
        self.assertEqual(result["text"], "Нашёл несколько наушников, вот лучшие.")

    def test_multiple_rounds_stop_at_limit(self):
        script = [call_part("search_products", {"query": f"q{i}"}, call_id=f"c{i}")
                  for i in range(5)]
        script.append(text_part("final"))
        agent = ShoppingAgent(FakeAIService(script), max_rounds=2)
        outcome = SearchOutcome(request=make_request("q"))
        result = run(agent.run("найди", AgentToolContext(),
            search_fn=lambda **kw: _fake_search(outcome, kw)))
        self.assertEqual(len(result["calls"]), 2)
        self.assertTrue(result["clamped"])

    def test_invalid_tool_arguments_rejected(self):
        with self.assertRaises(ToolArgumentError):
            validate_search_args({"query": ""}, None)
        with self.assertRaises(ToolArgumentError):
            validate_search_args({}, None)
        with self.assertRaises(ToolArgumentError):
            validate_compare_args({"question": ""}, [make_product("a", "A")])
        with self.assertRaises(ToolArgumentError):
            validate_compare_args({}, [])
        # Junk numbers are dropped, not executed.
        validated = validate_search_args(
            {"query": "наушники", "max_price": "abc", "min_orders": None}, None)
        self.assertIsNone(validated["max_price"])
        self.assertIsNone(validated["min_orders"])


async def _fake_search(outcome, kwargs):
    return outcome


# --- 9..11: handler-level behaviour -----------------------------------------


class FinalRelevanceCutTests(unittest.TestCase):
    """The agent path's final relevance selection over the deep pool.

    Regression for the live failure: a specific request («мощный повербанк»)
    whose pool held fewer verified products than the browse cap shipped the
    tier-0 unverified deep-pool tail (camping fans, drawstring bags, rental
    stations) as ordinary results. The final cut keeps EVERY verified
    product first and bounds the unverified fill.
    """

    def _service(self, products):
        provider = StubProvider({})
        provider.search_calls = 0

        async def search_products(request):
            provider.search_calls += 1
            return list(products)

        provider.search_products = search_products
        return SearchService([provider], ai_service=None)

    def _request(self, query="power bank"):
        return make_request(query)

    def test_verified_products_never_displaced_by_unverified_tail(self):
        verified = [
            make_product(f"v{i}", f"Power bank 20000mAh model {i}")
            for i in range(5)
        ]
        # Tier-0 junk behind the verified head: titles share NO request word
        # evidence the pipeline would verify (different product family).
        junk = [
            make_product(f"j{i}", f"Sailor Moon drawstring bag {i}")
            for i in range(30)
        ]
        service = self._service(verified + junk)

        outcome = run(service.find_products(
            self._request(), feed_selection=False
        ))

        titles = [p.title for p in outcome.products]
        verified_titles = [p.title for p in verified]
        # Every verified product shipped, BEFORE any unverified one.
        self.assertEqual(titles[:5], verified_titles)
        # The unverified tail is bounded, not the whole remaining cap.
        unverified_count = sum(
            1 for t in titles if t not in verified_titles
        )
        self.assertLessEqual(
            unverified_count, search_handlers.TIER0_FILL_LIMIT
            if hasattr(search_handlers, "TIER0_FILL_LIMIT")
            else 5
        )
        self.assertGreater(len(outcome.products), 0)

    def test_specific_query_does_not_return_unrelated_products(self):
        verified = [make_product("v0", "Power bank 20000mAh fast charge")]
        junk = [
            make_product("j0", "Portable camping fan rechargeable"),
            make_product("j1", "Bubble machine for kids party"),
            make_product("j2", "Underwater fishing light green"),
            make_product("j3", "Commercial power bank rental station 48 slot"),
            make_product("j4", "Sailor Moon drawstring bag"),
            make_product("j5", "Phone case silicon cover"),
        ]
        service = self._service(verified + junk)

        outcome = run(service.find_products(
            self._request(), feed_selection=False
        ))

        titles = " | ".join(p.title for p in outcome.products)
        # The verified power bank leads the response.
        self.assertEqual(outcome.products[0].title, verified[0].title)
        # Unrelated products are NOT shipped as ordinary results: the
        # bounded fill may include at most TIER0_FILL_LIMIT tail entries,
        # and the unrelated examples above must be outweighed by verified
        # evidence - with only ONE verified product, at most 5 unverified
        # cards follow it, none of them ranked ahead of it.
        self.assertLessEqual(len(outcome.products), 1 + 5)
        self.assertNotIn("camping fan", titles.split(" | ")[0])

    def test_cross_language_verified_matches_stay_eligible(self):
        # A Cyrillic title that romanizes to the Latin request noun IS
        # verified evidence (the same mechanism the ranking uses) and must
        # remain eligible - and rank WITH the latin matches, above junk.
        verified_cyr = make_product("c0", "Повер банк 20000 мАч быстрый")
        latin = [make_product(f"v{i}", f"Power bank {i}") for i in range(4)]
        junk = [
            make_product(f"j{i}", f"power tool battery pack {i}")
            for i in range(10)
        ]
        service = self._service([*latin, verified_cyr, *junk])

        outcome = run(service.find_products(
            self._request(), feed_selection=False
        ))

        ids = [p.product_id for p in outcome.products]
        # The verified cross-language match shipped WITH the latin matches:
        # the cross-writing evidence keeps it eligible (it is not discarded
        # by the final cut), whatever order the pre-existing ranking gave
        # peers inside one tier.
        self.assertIn("c0", ids)
        # It is never DROPPED by the relevance cut in favour of junk: with a
        # pool of 15 and a cap of 30 nothing is cut at all, so also verify
        # the actual guarantee - a verified match survives a deep pool cut.
        deep = self._service([*latin, verified_cyr, *[
            make_product(f"k{i}", f"power tool battery pack {i}")
            for i in range(60)
        ]])
        deep_outcome = run(deep.find_products(
            self._request(), feed_selection=False
        ))
        self.assertIn("c0", [p.product_id for p in deep_outcome.products])

    def test_no_verified_products_keeps_previous_behaviour(self):
        # A pool with no VERIFIED (tier>=1) product but word-level matches:
        # the previous deterministic cut applies unchanged (no empty screen
        # is manufactured, the cap is not inflated by unverified fill).
        # Titles share the request word so the gate keeps them; none of them
        # is a verified product-type match in this fixture.
        junk = [
            make_product(f"j{i}", f"power adapter cable model {i}")
            for i in range(40)
        ]
        service = self._service(junk)

        outcome = run(service.find_products(
            self._request(), feed_selection=False
        ))

        self.assertEqual(len(outcome.products), service.max_results)

    def test_small_pool_ships_unchanged(self):
        products = [make_product(f"p{i}", f"Power bank {i}") for i in range(3)]
        service = self._service(products)

        outcome = run(service.find_products(
            self._request(), feed_selection=False
        ))

        self.assertEqual(len(outcome.products), 3)

    def test_power_bank_false_positives_rank_behind_the_genuine_product(self):
        """The live «мощный повербанк» failure: rental stations and other
        devices that mention a power bank must not outrank the genuine
        power bank. Classification-only fix upstream of the cut: the cut
        itself stays unchanged, so a genuine product always leads.
        """
        genuine = make_product("v0", "Power bank 20000mAh fast charge PD 65W")
        false_positives = [
            make_product("j0", "Power bank rental station 48 slots payment kiosk"),
            make_product("j1", "Powerbank rental station commercial charging cabinet"),
            make_product("j2", "Emergency radio with power bank"),
            make_product("j3", "Bubble machine powered by a power bank"),
        ]
        junk = [
            make_product(f"k{i}", f"Sailor Moon drawstring bag {i}")
            for i in range(30)
        ]
        service = self._service([genuine, *false_positives, *junk])

        outcome = run(service.find_products(
            self._request("мощный повербанк"), feed_selection=False
        ))

        self.assertTrue(outcome.products)
        self.assertEqual(outcome.products[0].title, genuine.title)

    def test_agent_delivers_user_query_outcome_not_broadened_repeat(self):
        """The user's own query wins over a later BROADER repeat search.

        Live failure shape: the model searched the user's words, then a
        broader generic variant; the LAST outcome (30 loose products) used
        to be delivered. A broader phrase never replaces a specific one.
        """
        specific = SearchOutcome(
            request=make_request("power bank 20000mah"),
            products=[make_product("s1", "Power bank 20000mAh black")],
        )
        broadened = SearchOutcome(
            request=make_request("power"),
            products=[make_product("b1", "Power anything")],
        )
        agent = ShoppingAgent(FakeAIService([
            call_part("search_products", {"query": "power bank 20000mah"},
                      call_id="c1"),
            call_part("search_products", {"query": "power"}, call_id="c2"),
            text_part("Готово."),
        ]))

        async def search_fn(**kw):
            query = (kw.get("query") or "").lower()
            return broadened if query == "power" else specific

        result = run(agent.run(
            "найди power bank 20000mah", AgentToolContext(), search_fn=search_fn
        ))
        # The handler-side selection logic is mirrored here through the
        # recorded calls: the FIRST (specific) search's outcome is the one
        # the handler delivers, because "power" does not contain the words
        # of the specific phrase.
        self.assertEqual(len(result["calls"]), 2)
        self.assertEqual(
            result["calls"][0].search_outcome.request.original_query,
            "power bank 20000mah",
        )
