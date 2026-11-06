# -*- coding: utf-8 -*-
"""Blind evaluation corpus for the active Option C conversation interpreter.

This is a deterministic, network-free dataset definition. It does not call the
bot, Gemini, AliExpress, Telegram, or mutate application state.

Each turn contains the exact context snapshot sent to the interpreter and a
gold decision contract. Gold labels describe the intended conversational
decision, not a product-search result. Provider outcomes are represented as
post_search expectations where applicable.

Do not put real user identifiers, API credentials, or private chat transcripts
in this file. Add anonymized cases only after review.
"""

from __future__ import annotations


DATASET_VERSION = "consultant-dialogue-v1"
LANGUAGES = ("ru", "uk", "en")


def _context(
    *,
    active_slots=(),
    recent_dialogue=(),
    shown_products=(),
    pending_question=None,
    last_outcome=None,
):
    dialogue = [
        {"role": role, "text": text}
        for role, text in recent_dialogue
    ]
    return {
        "recent_turns": [item["text"] for item in dialogue if item["role"] == "user"][-6:],
        "recent_dialogue": dialogue[-12:],
        "active_slots": [
            {"kind": kind, "value": value, "status": "active"}
            for kind, value in active_slots
        ],
        "shown_product_count": len(shown_products),
        "shown_products": [
            {"id": pid, "title": title, "price": price, "currency": currency}
            for pid, title, price, currency in shown_products
        ],
        "last_outcome": last_outcome,
        "pending_question": pending_question,
    }


def _turn(text, action, *, context=None, product_type=None, slot_updates=(),
          retrieval=None, note=""):
    return {
        "user_text": text,
        "context": context or _context(),
        "expected": {
            "action": action,
            "product_type": product_type,
            "slot_updates": list(slot_updates),
            "retrieval": retrieval,
        },
        "note": note,
    }


DIALOGUE_CASES = [
    {
        "id": "D01",
        "scenario": "new_product_search",
        "language": "ru",
        "turns": [
            _turn(
                "Подбери беспроводную мышь для ноутбука с тихими кнопками",
                "new_search",
                product_type="мышь",
                retrieval=True,
                note="A concrete product and purpose are clear; do not ask an unnecessary question.",
            ),
        ],
    },
    {
        "id": "D02",
        "scenario": "budget_refinement",
        "language": "ru",
        "turns": [
            _turn(
                "Найди аккумуляторный шуруповёрт",
                "new_search",
                product_type="шуруповёрт",
                retrieval=True,
            ),
            _turn(
                "До 4000 гривен",
                "refine",
                context=_context(
                    active_slots=(("type", "шуруповёрт"),),
                    recent_dialogue=(
                        ("user", "Найди аккумуляторный шуруповёрт"),
                        ("assistant", "Подберу аккумуляторные шуруповёрты."),
                    ),
                ),
                retrieval=True,
                note="Budget is handled by the deterministic budget parser, not slot_updates.",
            ),
        ],
    },
    {
        "id": "D03",
        "scenario": "mandatory_feature_and_compatibility",
        "language": "uk",
        "turns": [
            _turn(
                "Потрібен павербанк для ноутбука з USB-C Power Delivery 65W",
                "new_search",
                product_type="павербанк",
                slot_updates=(
                    {"operation": "add", "kind": "use_case", "target": None, "value": "для ноутбука"},
                    {"operation": "add", "kind": "feature", "target": None, "value": "USB-C Power Delivery 65W"},
                ),
                retrieval=True,
                note="Preserve the actual product head and both the use case and explicit mandatory feature.",
            ),
        ],
    },
    {
        "id": "D04",
        "scenario": "exclusion_and_preference",
        "language": "ru",
        "turns": [
            _turn(
                "Только без активного шумоподавления, цвет чёрный",
                "refine",
                context=_context(
                    active_slots=(("type", "наушники"),),
                    recent_dialogue=(
                        ("user", "Найди беспроводные наушники"),
                        ("assistant", "Подберу беспроводные наушники."),
                    ),
                ),
                slot_updates=(
                    {"operation": "add", "kind": "exclusion", "target": None, "value": "без активного шумоподавления"},
                    {"operation": "add", "kind": "preference", "target": None, "value": "цвет чёрный"},
                ),
                retrieval=True,
            ),
        ],
    },
    {
        "id": "D05",
        "scenario": "correction_replaces_only_target_slot",
        "language": "ru",
        "turns": [
            _turn(
                "Не с двумя аккумуляторами, а с одним",
                "correction",
                context=_context(
                    active_slots=(
                        ("type", "шуруповёрт"),
                        ("feature", "с двумя аккумуляторами"),
                        ("feature", "с подсветкой"),
                    ),
                    recent_dialogue=(
                        ("user", "Найди шуруповёрт с двумя аккумуляторами и подсветкой"),
                        ("assistant", "Ищу варианты с двумя аккумуляторами и подсветкой."),
                    ),
                ),
                slot_updates=(
                    {"operation": "replace", "kind": "feature", "target": "с двумя аккумуляторами", "value": "с одним аккумулятором"},
                ),
                retrieval=True,
                note="The unrelated lighting requirement must remain active.",
            ),
        ],
    },
    {
        "id": "D06",
        "scenario": "explicit_product_switch",
        "language": "ru",
        "turns": [
            _turn(
                "Теперь ищу беспроводные наушники",
                "new_search",
                context=_context(
                    active_slots=(
                        ("type", "павербанк"),
                        ("feature", "USB-C 65W"),
                        ("budget", "до 2000 грн"),
                    ),
                    recent_dialogue=(
                        ("user", "Найди павербанк USB-C 65W до 2000 грн"),
                        ("assistant", "Подберу павербанки с USB-C 65W в этом бюджете."),
                    ),
                ),
                product_type="наушники",
                retrieval=True,
                note="A real new product head resets stale product constraints; do not keep power-bank slots.",
            ),
        ],
    },
    {
        "id": "D07",
        "scenario": "alternatives_same_product",
        "language": "en",
        "turns": [
            _turn(
                "Show me other options",
                "alternatives",
                context=_context(
                    active_slots=(("type", "wireless mouse"), ("feature", "silent clicks")),
                    recent_dialogue=(
                        ("user", "Find a wireless mouse with silent clicks"),
                        ("assistant", "Here are suitable wireless mouse options."),
                    ),
                    shown_products=(
                        ("mouse-01", "Wireless mouse silent click 2.4G", 12.5, "USD"),
                        ("mouse-02", "Bluetooth silent office mouse", 15.0, "USD"),
                    ),
                    last_outcome="ok",
                ),
                retrieval=True,
                note="Keep the same product family and exclude already-shown IDs in the engine.",
            ),
        ],
    },
    {
        "id": "D08",
        "scenario": "compare_shown_products",
        "language": "uk",
        "turns": [
            _turn(
                "Який із цих двох кращий для подорожей?",
                "compare",
                context=_context(
                    active_slots=(("type", "павербанк"),),
                    recent_dialogue=(
                        ("user", "Підбери павербанк для подорожей"),
                        ("assistant", "Ось два варіанти павербанків."),
                    ),
                    shown_products=(
                        ("power-01", "Power bank 20000mAh 65W", 38.0, "USD"),
                        ("power-02", "Power bank 10000mAh 30W compact", 24.0, "USD"),
                    ),
                    last_outcome="ok",
                ),
                retrieval=False,
                note="Compare the delivered cards; do not start a new product search.",
            ),
        ],
    },
    {
        "id": "D09",
        "scenario": "factual_question_about_shown_item",
        "language": "ru",
        "turns": [
            _turn(
                "У второго есть защита от воды?",
                "compare",
                context=_context(
                    active_slots=(("type", "портативная колонка"),),
                    recent_dialogue=(
                        ("user", "Найди портативную колонку"),
                        ("assistant", "Вот несколько подходящих колонок."),
                    ),
                    shown_products=(
                        ("speaker-01", "Portable speaker IPX7 waterproof", 29.0, "USD"),
                        ("speaker-02", "Mini bluetooth speaker 5W", 18.0, "USD"),
                    ),
                    last_outcome="ok",
                ),
                retrieval=False,
                note="Route to grounded product evidence; never infer IP protection from product category.",
            ),
        ],
    },
    {
        "id": "D10",
        "scenario": "short_ambiguous_message",
        "language": "ru",
        "turns": [
            _turn(
                "Для машины",
                "clarify",
                context=_context(),
                retrieval=False,
                note="There is no active product or task; ask one useful question instead of inventing a product.",
            ),
        ],
    },
    {
        "id": "D11",
        "scenario": "no_candidates_after_search",
        "language": "en",
        "turns": [
            _turn(
                "Find a compact portable air purifier for a very small room",
                "new_search",
                product_type="air purifier",
                retrieval=True,
                note="Interpreter should authorize a search; the no-results outcome happens after retrieval.",
            ),
        ],
        "post_search": {
            "provider_outcome": "success_empty_or_no_eligible",
            "expected_operations": ["no_results_reply"],
            "must_not_invent_products": True,
        },
    },
    {
        "id": "D12",
        "scenario": "aliexpress_provider_failure",
        "language": "en",
        "turns": [
            _turn(
                "Find a USB-C wall charger with two ports",
                "new_search",
                product_type="charger",
                retrieval=True,
                note="Provider failure is downstream of the interpreter and must not be mislabeled as no catalogue matches.",
            ),
        ],
        "post_search": {
            "provider_outcome": "total_failure",
            "expected_operations": [],
            "expected_user_outcome": "honest_provider_failure",
            "must_not_invent_products": True,
        },
    },
    {
        "id": "D13",
        "scenario": "long_multiturn_dialogue",
        "language": "ru",
        "turns": [
            _turn(
                "Подбери компрессор для гаража",
                "clarify",
                context=_context(),
                retrieval=False,
                note="Garage use alone does not distinguish tire inflation from painting or other compressor tasks; ask one focused question.",
            ),
            _turn(
                "Нужен для подкачки автомобильных шин, не для покраски",
                "refine",
                context=_context(
                    active_slots=(("type", "компрессор"),),
                    recent_dialogue=(
                        ("user", "Подбери компрессор для гаража"),
                        ("assistant", "Для какой задачи нужен компрессор?"),
                    ),
                    pending_question="Для какой задачи нужен компрессор?",
                    last_outcome="clarify",
                ),
                slot_updates=(
                    {"operation": "add", "kind": "use_case", "target": None, "value": "для подкачки автомобильных шин"},
                    {"operation": "add", "kind": "exclusion", "target": None, "value": "не для покраски"},
                ),
                retrieval=True,
                note="Resolve the assistant's previous clarification in context; do not search for a new product type.",
            ),
            _turn(
                "И до 3000 грн",
                "refine",
                context=_context(
                    active_slots=(
                        ("type", "компрессор"),
                        ("use_case", "для подкачки автомобильных шин"),
                        ("exclusion", "не для покраски"),
                    ),
                    recent_dialogue=(
                        ("user", "Подбери компрессор для гаража"),
                        ("assistant", "Для какой задачи нужен компрессор?"),
                        ("user", "Нужен для подкачки автомобильных шин, не для покраски"),
                        ("assistant", "Понял, подберу компрессор для шин."),
                    ),
                    last_outcome="ok",
                ),
                retrieval=True,
                note="Keep the clarified task and apply the new budget.",
            ),
            _turn(
                "И чтобы не слишком шумный",
                "refine",
                context=_context(
                    active_slots=(
                        ("type", "компрессор"),
                        ("use_case", "для подкачки автомобильных шин"),
                        ("exclusion", "не для покраски"),
                    ),
                    recent_dialogue=(
                        ("user", "Подбери компрессор для гаража"),
                        ("assistant", "Для какой задачи нужен компрессор?"),
                        ("user", "Нужен для подкачки автомобильных шин, не для покраски"),
                        ("assistant", "Понял, подберу компрессор для шин."),
                        ("user", "И до 3000 грн"),
                        ("assistant", "Учту бюджет до 3000 грн."),
                    ),
                    last_outcome="ok",
                ),
                slot_updates=(
                    {"operation": "add", "kind": "preference", "target": None, "value": "не слишком шумный"},
                ),
                retrieval=True,
                note="A soft preference is added without dropping the task, exclusion, or budget.",
            ),
        ],
    },
]


# This is a contract check for the corpus itself, not a model-quality score.
def validate_dataset() -> list[str]:
    errors: list[str] = []
    ids = [case.get("id") for case in DIALOGUE_CASES]
    if len(ids) != len(set(ids)):
        errors.append("duplicate case id")
    if len(DIALOGUE_CASES) != 13:
        errors.append(f"expected 13 scenarios, found {len(DIALOGUE_CASES)}")
    seen_languages = {case.get("language") for case in DIALOGUE_CASES}
    if not set(LANGUAGES).issubset(seen_languages):
        errors.append("dataset must include RU, UK and EN")
    for case in DIALOGUE_CASES:
        if not case.get("turns"):
            errors.append(f"{case.get('id')}: no turns")
        for index, turn in enumerate(case.get("turns", []), start=1):
            expected = turn.get("expected", {})
            if expected.get("action") not in {
                "new_search", "refine", "correction", "alternatives",
                "compare", "clarify", "respond",
            }:
                errors.append(f"{case.get('id')} turn {index}: invalid action")
            if expected.get("action") == "new_search" and not expected.get("product_type"):
                errors.append(f"{case.get('id')} turn {index}: new_search needs product_type")
            if expected.get("action") != "new_search" and expected.get("product_type") is not None:
                errors.append(f"{case.get('id')} turn {index}: non-search has product_type")
            context = turn.get("context", {})
            if not isinstance(context.get("recent_dialogue"), list):
                errors.append(f"{case.get('id')} turn {index}: recent_dialogue must be a list")
            if len(context.get("recent_dialogue", [])) > 12:
                errors.append(f"{case.get('id')} turn {index}: recent_dialogue exceeds runtime limit")
            for update in expected.get("slot_updates", []):
                if update.get("operation") not in {"add", "replace", "remove"}:
                    errors.append(f"{case.get('id')} turn {index}: invalid slot operation")
    return errors


__all__ = [
    "DATASET_VERSION",
    "LANGUAGES",
    "DIALOGUE_CASES",
    "validate_dataset",
]
