"""Offline regression tests for initial product-card image delivery."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import InputFile

from handlers import common, search
from models.product import Marketplace, Product


class _FakeResponse:
    headers = {"content-type": "image/jpeg", "content-length": "4"}
    history = []
    url = "https://ae-pic-a1.aliexpress-media.com/kf/product.jpg"

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self):
        yield b"\xff\xd8\xff\xd9"


class _FakeStream:
    async def __aenter__(self):
        return _FakeResponse()

    async def __aexit__(self, *_args):
        return False


class _FakeHttpClient:
    def __init__(self, **_kwargs) -> None:
        self.request = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def stream(self, method, url, **kwargs):
        self.request = (method, url, kwargs)
        return _FakeStream()


class _FakeBot:
    def __init__(self) -> None:
        self.photos = []
        self.messages = []

    async def send_photo(self, chat_id, *, photo, **kwargs) -> None:
        if isinstance(photo, str):
            raise RuntimeError("Telegram could not fetch the remote URL")
        self.photos.append((chat_id, photo, kwargs))

    async def send_message(self, chat_id, text, **kwargs) -> None:
        self.messages.append((chat_id, text, kwargs))


class _FakeApplication:
    bot_data = {}


class _FakeContext:
    def __init__(self) -> None:
        self.bot = _FakeBot()
        self.application = _FakeApplication()


def _product() -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id="image-test-1",
        title="Тестовый товар",
        product_url="https://www.aliexpress.com/item/123.html",
        image_url="https://ae-pic-a1.aliexpress-media.com/kf/product.jpg",
        price=10.0,
        currency="USD",
    )


class ProductCardImageDeliveryTests(unittest.TestCase):
    def test_initial_card_retries_failed_remote_photo_as_uploaded_bytes(self) -> None:
        context = _FakeContext()
        fake_client = _FakeHttpClient()

        with patch("handlers.common.httpx.AsyncClient", return_value=fake_client):
            asyncio.run(
                common.send_product_card(
                    context,
                    123,
                    _product(),
                    index=1,
                )
            )

        self.assertEqual(len(context.bot.photos), 1)
        self.assertIsInstance(context.bot.photos[0][1], InputFile)
        self.assertEqual(context.bot.photos[0][0], 123)
        self.assertEqual(context.bot.messages, [])
        self.assertEqual(fake_client.request[0], "GET")
        self.assertEqual(
            fake_client.request[1],
            "https://ae-pic-a1.aliexpress-media.com/kf/product.jpg",
        )

    def test_initial_image_card_replaces_text_origin_with_photo_card(self) -> None:
        context = SimpleNamespace()
        query = SimpleNamespace(message=SimpleNamespace(photo=None))
        product = _product()
        outcome = SimpleNamespace(products=[product])
        keyboard = object()

        with (
            patch("handlers.search.common.get_outcome_meta", return_value={}),
            patch(
                "handlers.search.common.is_product_tracked",
                new=AsyncMock(return_value=False),
            ),
            patch("handlers.search._browse_keyboard", return_value=keyboard),
            patch("handlers.search._card_caption", return_value="header"),
            patch("handlers.search.common.product_card_text", return_value="caption"),
            patch(
                "handlers.search._replace_browsed_card",
                new=AsyncMock(),
            ) as replace_card,
            patch("handlers.search.common.record_event", new=AsyncMock()),
            patch("handlers.search.common.remember_shown", new=AsyncMock()),
            patch("handlers.search.common.remember_views", new=AsyncMock()),
            patch("handlers.search.common.session_flow", return_value="themes:test"),
        ):
            delivered = asyncio.run(
                search._send_product_cards(
                    context,
                    123,
                    outcome,
                    "session-1",
                    user_id=1,
                    telegram_id=1,
                    origin_query=query,
                    strict_navigation=True,
                )
            )

        self.assertTrue(delivered)
        replace_card.assert_awaited_once_with(
            query,
            context,
            product,
            0,
            "caption",
            keyboard,
            send_first=True,
        )

    def test_non_aliexpress_image_host_is_not_downloaded(self) -> None:
        context = _FakeContext()
        product = _product()
        product.image_url = "https://example.com/product.jpg"

        with patch("handlers.common.httpx.AsyncClient") as client_factory:
            asyncio.run(
                common.send_product_card(context, 123, product, index=1)
            )

        client_factory.assert_not_called()
        self.assertEqual(context.bot.photos, [])
        self.assertEqual(len(context.bot.messages), 1)

