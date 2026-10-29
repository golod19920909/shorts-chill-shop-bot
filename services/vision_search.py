"""Vision AI prompt and structured response parser for photo search.

Converts an uploaded photo into:
- 2-3 broad, basic Russian consumer keywords (query)
- A broad category name (fallback_category)
for querying AliExpress.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

VISION_SEARCH_PROMPT = """Посмотри на фотографию товара и определи, что это за вещь.

Верни ТОЛЬКО валидный JSON-объект (без markdown, без пояснений) строго следующей структуры:
{
  "query": "2-3 простых, широких русских поисковых слова для покупателя (например, 'спиннинг рыболовный', 'удилище')",
  "fallback_category": "широкая общая категория товара (например, 'рыболовные снасти', 'автотовары')"
}

Правила:
- В поле 'query': ровно 2–3 базовых, общеупотребительных потребительских слова на русском языке, как обычный покупатель ищет товар на маркетплейсе.
- СТРОГО ЗАПРЕЩЕНО: узкий OEM-жаргон, редкие артикулы, экзотический сленг, технические спецификации или слишком длинные узкие описания.
- В поле 'fallback_category': широкое название товарной категории на русском языке.
"""


def parse_vision_response(raw_text: str | None) -> dict[str, str] | None:
    """Parse and validate vision response containing query and fallback_category."""
    if not raw_text or not isinstance(raw_text, str):
        return None

    cleaned = raw_text.strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(0)

    try:
        data = json.loads(cleaned)
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    query = str(data.get("query") or "").strip()
    fallback_category = str(data.get("fallback_category") or "").strip()

    if not query:
        return None

    return {
        "query": query,
        "fallback_category": fallback_category,
    }


async def analyze_photo_with_vision(
    image_bytes: bytes, ai_service: Any = None
) -> dict[str, str] | None:
    """Analyze photo using Gemini Vision model and return query + fallback_category."""
    if ai_service is None or not getattr(ai_service, "is_available", False):
        return None

    client = getattr(ai_service, "_client", None)
    if client is None:
        return None

    try:
        from google.genai import types as genai_types
    except Exception:
        logger.exception("Photo analysis unavailable: genai SDK is missing")
        return None

    vision_model = getattr(ai_service, "vision_model", "gemini-3.5-flash-lite")

    try:
        generator = getattr(ai_service, "_generate", None)
        if callable(generator):
            raw = await generator(
                [
                    genai_types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                    VISION_SEARCH_PROMPT,
                ],
                models=(vision_model,),
            )
        else:
            raw = None
        return parse_vision_response(raw)
    except Exception:
        logger.exception("Failed to analyze photo with vision prompt")
        return None
