"""Tests for grounded semantic review of marketplace title fragments."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from models.product import Marketplace, Product
from services.ai_service import AIService


class SemanticIdentityPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_comma_does_not_automatically_mean_a_second_product(self) -> None:
        service = AIService(api_key=None)
        service._client = object()
        candidate = Product(
            marketplace=Marketplace.ALIEXPRESS,
            product_id="spinning-rod-1",
            title=(
                "Телескопическая удочка Goture RIGEL 1,8-2,7 м, "
                "спиннинг из углеродного волокна для заброса"
            ),
            product_url="https://www.aliexpress.com/item/spinning-rod-1.html",
            price=1300,
            currency="UAH",
        )
        service._generate = AsyncMock(return_value=(
            '{"selections":[{"id":"spinning-rod-1",'
            '"identity_evidence":"спиннинг из углеродного волокна",'
            '"requirement_evidence":{}}]}'
        ))

        selected = await service.select_conversation_candidates(
            [candidate],
            query="спиннинг",
            wanted=1,
            identity_review_ids=[candidate.product_id],
        )

        self.assertEqual(selected, [candidate.product_id])
        prompt = service._generate.await_args.args[0]
        self.assertIn("Не считай запятую сама по себе признаком второго товара", prompt)
        self.assertIn("отклони карточку", prompt)
