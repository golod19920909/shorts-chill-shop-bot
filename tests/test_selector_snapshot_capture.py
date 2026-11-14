"""Opt-in selector snapshot capture is privacy-bounded and non-invasive."""

import asyncio
import json

from models.product import Marketplace, Product
from services.ai_service import AIService


def test_capture_is_disabled_without_explicit_environment_opt_in(tmp_path, monkeypatch):
    monkeypatch.delenv("SHORTS_CHILL_SELECTOR_CAPTURE", raising=False)
    monkeypatch.setenv("SHORTS_CHILL_SELECTOR_CAPTURE_DIR", str(tmp_path))

    result = AIService._write_selector_capture(
        candidate_payloads=[],
        query="найди павербанк",
        wanted=20,
        required_features=[],
        identity_review_ids=[],
        marketplace_terms=[],
        budget_constraint=None,
    )

    assert result is None
    assert list(tmp_path.iterdir()) == []


def test_capture_writes_exact_selector_fields_without_urls_or_user_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("SHORTS_CHILL_SELECTOR_CAPTURE", "1")
    monkeypatch.setenv("SHORTS_CHILL_SELECTOR_CAPTURE_DIR", str(tmp_path))

    path = AIService._write_selector_capture(
        candidate_payloads=[
            {
                "id": "product-1",
                "title": "USB-C power bank 65W",
                "description": "Power Delivery 65W",
                "category_path": ["Power Banks"],
                "price": 29.99,
                "currency": "USD",
                "discount_percent": 10,
                "rating": 4.8,
                "positive_rate": 98.5,
                "orders_count": 250,
                "has_image": True,
                "availability": "unknown",
                "affiliate_url": "must-not-be-captured",
                "telegram_user_id": "must-not-be-captured",
            }
        ],
        query="павербанк USB-C 65W",
        wanted=20,
        required_features=["USB-C Power Delivery 65W"],
        identity_review_ids=["product-1"],
        marketplace_terms=["65W USB-C power bank"],
        budget_constraint="до 1500 грн",
    )

    assert path is not None
    snapshot = json.loads(open(path, encoding="utf-8").read())
    assert snapshot["source"]["provider"] == "AliExpress Affiliate API"
    assert snapshot["source"]["capture_kind"] == "selector_input_capture"
    assert snapshot["source"]["query_language"] == "mixed"
    assert snapshot["source"]["page_count"] is None
    assert snapshot["source"]["candidate_count"] == 1
    assert snapshot["request"]["query"] == "павербанк USB-C 65W"
    assert snapshot["request"]["required_features"] == ["USB-C Power Delivery 65W"]
    assert snapshot["gold"] is None
    assert snapshot["candidates"][0]["id"] == "product-1"
    assert snapshot["candidates"][0]["positive_rate"] == 98.5
    assert snapshot["candidates"][0]["has_image"] is True
    assert "affiliate_url" not in snapshot["candidates"][0]
    assert "telegram_user_id" not in snapshot["candidates"][0]



def test_selector_method_writes_capture_with_normalized_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("SHORTS_CHILL_SELECTOR_CAPTURE", "1")
    monkeypatch.setenv("SHORTS_CHILL_SELECTOR_CAPTURE_DIR", str(tmp_path))

    service = AIService(None)
    service._client = object()

    async def fake_generate(*args, **kwargs):
        return '{"selections": []}'

    monkeypatch.setattr(service, "_generate", fake_generate)
    product = Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id="product-1",
        title="USB-C power bank 65W",
        product_url="https://example.invalid/product-1",
        description="Power Delivery 65W",
    )

    result = asyncio.run(
        service.select_conversation_candidates(
            [product],
            query="павербанк USB-C 65W",
            budget_constraint="  до   1500 грн  ",
        )
    )

    assert result == []
    captures = list(tmp_path.glob("selector_*.json"))
    assert len(captures) == 1
    snapshot = json.loads(captures[0].read_text(encoding="utf-8"))
    assert snapshot["request"]["budget_constraint"] == "до 1500 грн"
    assert snapshot["candidates"][0]["id"] == "product-1"
