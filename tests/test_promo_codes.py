"""Tests for the standalone «🎟 Промокоды» section.

The section is ONE screen with ONE destination: the configured AliExpress
partner/referral page (``config.promo.PROMO_REFERRAL_URL``), opened by a
Telegram URL button. These tests pin that contract:

1. the main-menu button dispatches to the section;
2. the screen offers exactly ONE destination and it is the configured URL
   byte-for-byte - nothing appended, nothing replaced by a plain
   marketplace link;
3. that destination is a URL button: pressing it sends no callback to the
   bot, so no service, no API call and no URL rebuild can happen;
4. no campaign list, campaign id, promo service or featuredpromo call is
   involved anywhere in the section;
5. the URL is defined in EXACTLY one place (``config/promo.py``) and is read
   from there at render time, so a restart cannot change it;
6. buttons of the removed campaign list (``pcamp:``) are tombstoned: such a
   press is answered once and the message becomes the current section.

Product-level promo data is a different feature and stays covered by
``tests/test_product_promo_card.py``.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from config.promo import PROMO_REFERRAL_URL
from handlers import promo_codes
from handlers.start import dispatch_menu
from keyboards.main import (
    CB_BACK,
    CB_NAV_BACK,
    CB_PROMO_CAMPAIGN_LEGACY_PREFIX,
    CB_PROMO_CODES,
    LABEL_PROMO_CODES,
    MAIN_MENU_CALLBACKS,
)
from keyboards.promo_codes import LABEL_PROMO_OPEN, promo_codes_keyboard

ROOT = Path(__file__).resolve().parents[1]

#: The exact destination the «🎟 Промокоды» button must open. Pinned here on
#: purpose: the requirement is that ONE configured referral URL is used, so a
#: silent replacement by another link has to fail this test.
EXPECTED_PROMO_URL = "https://s.click.aliexpress.com/e/_c42dRSbf"


def source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def run(coro):
    return asyncio.run(coro)


def buttons(markup) -> list:
    return [button for row in markup.inline_keyboard for button in row]


def urls_of(markup) -> list[str]:
    return [button.url for button in buttons(markup) if button.url]


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str, dict]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.messages.append((chat_id, text, kwargs))


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[tuple[str, dict]] = []

    async def reply_text(self, text: str, **kwargs) -> None:
        self.replies.append((text, kwargs))


class FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.answers: list[tuple[str | None, bool]] = []
        self.edits: list[tuple[str, dict]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text: str, **kwargs) -> None:
        self.edits.append((text, kwargs))


class FakeChat:
    id = 47


class FakeUser:
    def __init__(self, user_id: int = 900) -> None:
        self.id = user_id
        self.username = f"user{user_id}"
        self.first_name = f"User {user_id}"


class FakeUpdate:
    def __init__(
        self,
        query: FakeQuery | None = None,
        message: FakeMessage | None = None,
        user_id: int = 900,
    ) -> None:
        self.callback_query = query
        self.effective_message = message
        self.effective_chat = FakeChat()
        self.effective_user = FakeUser(user_id)


class FakeApplication:
    def __init__(self, bot_data: dict | None = None) -> None:
        self.bot_data = bot_data or {}


class FakeContext:
    def __init__(self, bot_data: dict | None = None) -> None:
        self.application = FakeApplication(bot_data)
        self.bot = FakeBot()



class PromoSectionContractTests(unittest.TestCase):
    """The section has exactly one, fixed destination."""

    def test_the_screen_has_one_destination_and_it_is_the_required_url(self):
        markup = promo_codes_keyboard()

        self.assertEqual(urls_of(markup), [EXPECTED_PROMO_URL])
        self.assertEqual(urls_of(markup), [PROMO_REFERRAL_URL])

    def test_the_url_button_is_labelled_and_carries_no_callback(self):
        # A URL button is opened by Telegram itself. No callback data means
        # no handler, no service call and no chance to rebuild the link.
        url_button = next(b for b in buttons(promo_codes_keyboard()) if b.url)

        self.assertEqual(url_button.text, LABEL_PROMO_OPEN)
        self.assertIsNone(url_button.callback_data)

    def test_back_and_home_navigation_is_present(self):
        callbacks = [
            button.callback_data
            for button in buttons(promo_codes_keyboard())
            if button.callback_data
        ]

        self.assertEqual(callbacks, [CB_NAV_BACK, CB_BACK])

    def test_the_menu_button_is_the_configured_section_button(self):
        self.assertIn(CB_PROMO_CODES, MAIN_MENU_CALLBACKS)
        self.assertEqual(LABEL_PROMO_CODES, "\U0001F39F Промокоды")

    def test_the_url_is_defined_in_exactly_one_place(self):
        # No second source of the link: a runtime-built URL would be an
        # uncontrolled destination, so the literal lives only in config.
        offenders = [
            path.relative_to(ROOT).as_posix()
            for path in ROOT.rglob("*.py")
            if ".venv" not in path.parts
            and "tests" not in path.parts
            and PROMO_REFERRAL_URL in path.read_text(encoding="utf-8")
        ]

        self.assertEqual(offenders, ["config/promo.py"])

    def test_the_handler_builds_no_link_and_calls_no_external_service(self):
        handler = source("handlers/promo_codes.py")

        for banned in (
            "http://",
            "https://",
            "s.click",
            "url=",
            "from services",
            "promo_service",
            "featuredpromo",
            "get_featured_promo",
            "parse_promo_campaigns",
            "promo_campaigns",
        ):
            self.assertNotIn(banned, handler, f"handler must not contain {banned}")

    def test_the_removed_campaign_plumbing_cannot_come_back(self):
        for path in (
            "handlers/promo_codes.py",
            "keyboards/promo_codes.py",
            "config/promo.py",
        ):
            text = source(path)
            for banned in ("PromoCodesService", "PromoCampaign", "campaign_id"):
                self.assertNotIn(banned, text, f"{path} must not contain {banned}")

        with self.assertRaises(ImportError):
            __import__("services.promo_codes")
        with self.assertRaises(ImportError):
            __import__("models.promo")


class PromoHandlerTests(unittest.TestCase):
    """Rendering the section and surviving old messages."""

    def test_menu_button_dispatches_to_the_section(self):
        query = FakeQuery(CB_PROMO_CODES)
        handled = run(dispatch_menu(CB_PROMO_CODES, FakeUpdate(query), FakeContext()))

        self.assertTrue(handled)
        text, kwargs = query.edits[-1]
        self.assertEqual(text, promo_codes.PROMO_TEXT)
        self.assertEqual(urls_of(kwargs["reply_markup"]), [PROMO_REFERRAL_URL])
        self.assertEqual(query.answers, [(None, False)])

    def test_the_section_needs_no_bot_data_at_all(self):
        # No database, no search service, no promo service: the screen is
        # static config plus a keyboard, so an API outage cannot break it.
        query = FakeQuery(CB_PROMO_CODES)
        run(promo_codes.show_promo_codes(FakeUpdate(query), FakeContext({})))

        self.assertEqual(len(query.edits), 1)
        self.assertEqual(query.answers, [(None, False)])

    def test_a_plain_message_request_is_answered_with_the_section(self):
        message = FakeMessage()
        run(
            promo_codes.show_promo_codes(FakeUpdate(None, message), FakeContext({}))
        )

        text, kwargs = message.replies[-1]
        self.assertEqual(text, promo_codes.PROMO_TEXT)
        self.assertEqual(urls_of(kwargs["reply_markup"]), [PROMO_REFERRAL_URL])

    def test_legacy_campaign_press_is_answered_once_and_rerenders(self):
        query = FakeQuery(f"{CB_PROMO_CAMPAIGN_LEGACY_PREFIX}:0")
        run(promo_codes.promo_legacy_callback(FakeUpdate(query), FakeContext({})))

        # Answered exactly once: a second answer() makes Telegram reject it.
        self.assertEqual(
            query.answers, [(promo_codes.LEGACY_CAMPAIGN_ANSWER, True)]
        )
        text, kwargs = query.edits[-1]
        self.assertEqual(text, promo_codes.PROMO_TEXT)
        self.assertEqual(urls_of(kwargs["reply_markup"]), [PROMO_REFERRAL_URL])
        self.assertIn("кампаний", promo_codes.LEGACY_CAMPAIGN_ANSWER)

    def test_legacy_press_without_a_callback_query_is_a_no_op(self):
        message = FakeMessage()
        run(
            promo_codes.promo_legacy_callback(
                FakeUpdate(None, message), FakeContext({})
            )
        )

        self.assertEqual(message.replies, [])

    def test_the_legacy_handler_is_registered_on_the_legacy_prefix(self):
        wiring = source("bot/__init__.py")

        self.assertIn("promo_legacy_callback", wiring)
        self.assertIn('f"^{CB_PROMO_CAMPAIGN_LEGACY_PREFIX}:"', wiring)
        # The removed campaign-callback prefix is not built anywhere.
        self.assertNotIn(
            "CB_PROMO_CAMPAIGN_PREFIX", source("keyboards/promo_codes.py")
        )


if __name__ == "__main__":
    unittest.main()

