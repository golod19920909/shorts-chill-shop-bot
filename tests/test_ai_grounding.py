from __future__ import annotations

import unittest

from services.ai_service import AIService


class ProductAnswerGroundingTests(unittest.TestCase):
    def test_unreported_categorical_feature_is_rejected(self):
        answer = AIService._validated_sentences(
            None,
            "Да, он водонепроницаемый.",
            {"title": "Bluetooth speaker", "price": 12.0, "currency": "USD"},
        )
        self.assertIsNone(answer)

    def test_fact_backed_attribute_is_kept(self):
        answer = AIService._validated_sentences(
            None,
            "В карточке указано: Bluetooth.",
            {"title": "Bluetooth speaker", "price": 12.0, "currency": "USD"},
        )
        self.assertEqual(answer, "В карточке указано: Bluetooth.")

    def test_unsupported_sentence_is_removed_without_losing_supported_sentence(self):
        answer = AIService._validated_sentences(
            None,
            "В карточке указано Bluetooth. Корпус водонепроницаемый.",
            {"title": "Bluetooth speaker"},
        )
        self.assertEqual(answer, "В карточке указано Bluetooth.")


if __name__ == "__main__":
    unittest.main()
