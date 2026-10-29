# -*- coding: utf-8 -*-
"""
B5: bounded evaluation dataset for the search-pipeline quality review.

READ-ONLY fixture: only DEFINES queries + expected outcome hints. Never runs
the bot, never touches the network, never mutates production code.

~30-40 controlled queries, Russian primary with Ukrainian and English cases,
spanning every required intent category (short / long / numeric / ambiguous /
discovery / clarification / language). Each case carries an
``expected_outcome_hint`` used by the B5 runner to score PASS / FAIL /
INCONCLUSIVE. The hint is a quality expectation, NOT a hard assertion on
relevance ranking - search semantics are intentionally not changed by B5.

No query text is stored by the diagnostics registry (privacy rule), and this
fixture contains no secrets or personal identifiers.
"""

from __future__ import annotations

# Outcome labels (mirrors services.search_diagnostics constants).
OUTCOME_SUCCESS_WITH_RESULTS = "SUCCESS_WITH_RESULTS"
OUTCOME_SUCCESS_EMPTY_RAW = "SUCCESS_EMPTY_RAW"
OUTCOME_EMPTY_AFTER_RELEVANCE = "EMPTY_AFTER_RELEVANCE"
OUTCOME_EMPTY_AFTER_CONSTRAINTS = "EMPTY_AFTER_CONSTRAINTS"
OUTCOME_PROVIDER_TOTAL_FAILURE = "PROVIDER_TOTAL_FAILURE"


def _q(
    qid: str,
    category: str,
    language: str,
    query: str,
    expected_outcome_hint: str,
    intent_category: str,
    notes: str = "",
) -> dict:
    base = {
        "id": qid,
        "category": category,
        "language": language,
        "query": query,
        "expected_outcome_hint": expected_outcome_hint,
        "intent_category": intent_category,
        "notes": notes,
    }
    return base


# -- dataset ------------------------------------------------------------------
# Order is fixed for reproducibility. IDs are stable across runs.
B5_DATASET: list[dict] = [
    # -- short product queries (Russian) --------------------------------------
    _q(
        "B5-01", "short", "ru", "компрессор",
        OUTCOME_SUCCESS_WITH_RESULTS, "product",
        "single-noun product; expects real listings",
    ),
    _q(
        "B5-02", "short", "ru", "повербанк",
        OUTCOME_SUCCESS_WITH_RESULTS, "product",
        "single-noun product; expects real listings",
    ),
    _q(
        "B5-03", "short", "ru", "кофемолка",
        OUTCOME_SUCCESS_WITH_RESULTS, "product",
        "single-noun product; expects real listings",
    ),
    _q(
        "B5-04", "short", "ru", "дрель",
        OUTCOME_SUCCESS_WITH_RESULTS, "product",
        "single-noun product; expects real listings",
    ),
    _q(
        "B5-05", "short", "ru", "чехол для iPhone 15",
        OUTCOME_SUCCESS_WITH_RESULTS, "product",
        "product + compatibility target; expects case listings",
    ),

    # -- long / descriptive product queries (Russian) -------------------------
    _q(
        "B5-06", "long", "ru",
        "беспроводная мышь для ноутбука тихая с хорошей батареей",
        OUTCOME_SUCCESS_WITH_RESULTS, "product",
        "multi-attribute product query; expects mouse listings",
    ),
    _q(
        "B5-07", "long", "ru",
        "зимняя куртка мужская водонепроницаемая с капюшоном размер L",
        OUTCOME_SUCCESS_WITH_RESULTS, "product",
        "clothing with several attributes; expects jacket listings",
    ),
    _q(
        "B5-08", "long", "ru",
        "робот-пылесос с fonctions влажной уборки",
        OUTCOME_SUCCESS_WITH_RESULTS, "product",
        "appliance with feature; expects robot-vacuum listings",
    ),
    _q(
        "B5-09", "long", "ru",
        "фильтр для воды под мойку",
        OUTCOME_SUCCESS_WITH_RESULTS, "product",
        "compatibility target (sink); expects water-filter listings",
    ),
    _q(
        "B5-10", "long", "ru",
        "ищу наушники без проводов и без шумоподавления",
        OUTCOME_EMPTY_AFTER_RELEVANCE, "product",
        "negation-heavy query; may empty after relevance (noise-cancellation "
        "exclusion is a genuine exclusion).",
    ),

    # -- numeric / spec queries -----------------------------------------------
    _q(
        "B5-11", "numeric", "ru", "монитор 27 дюймов 144 Гц",
        OUTCOME_SUCCESS_WITH_RESULTS, "product",
        "numeric specs; expects 27-inch 144Hz monitor listings",
    ),
    _q(
        "B5-12", "numeric", "ru", "SSD 2 ТБ SATA",
        OUTCOME_SUCCESS_WITH_RESULTS, "product",
        "capacity + interface spec; expects SSD listings",
    ),
    _q(
        "B5-13", "numeric", "ru", "автомобильный компрессор 12 В с манометром",
        OUTCOME_SUCCESS_WITH_RESULTS, "product",
        "voltage + accessory spec; expects car-compressor listings",
    ),
    _q(
        "B5-14", "numeric", "ru", "шланг поливочный 20 метров с распылителем",
        OUTCOME_SUCCESS_WITH_RESULTS, "product",
        "length + accessory; expects garden-hose listings",
    ),
    _q(
        "B5-15", "numeric", "ru", "гантели разборные 2x20 кг",
        OUTCOME_SUCCESS_WITH_RESULTS, "product",
        "weight spec; expects dumbbell listings",
    ),

    # -- ambiguous / clarification (must NOT invent products) -----------------
    _q(
        "B5-16", "ambiguous", "ru", "хороший подарок",
        OUTCOME_SUCCESS_EMPTY_RAW, "ambiguous",
        "no concrete product category; expects empty raw",
    ),
    _q(
        "B5-17", "ambiguous", "ru", "что-нибудь для машины",
        OUTCOME_SUCCESS_EMPTY_RAW, "discovery",
        "mood/discovery request; no product type; expects empty raw",
    ),
    _q(
        "B5-18", "clarification", "ru", "устройство",
        OUTCOME_SUCCESS_EMPTY_RAW, "ambiguous",
        "bare generic noun; expects empty raw",
    ),
    _q(
        "B5-19", "clarification", "ru", "лучшее",
        OUTCOME_SUCCESS_EMPTY_RAW, "ambiguous",
        "superlative junk only; expects empty raw",
    ),
    _q(
        "B5-20", "clarification", "ru", "оно",
        OUTCOME_SUCCESS_EMPTY_RAW, "clarification",
        "pronoun only; expects empty raw",
    ),

    # -- language: Russian / Ukrainian / English / mixed ----------------------
    _q(
        "B5-21", "language", "ru", "наушники беспроводные блютуз",
        OUTCOME_SUCCESS_WITH_RESULTS, "product",
        "Russian + transliterated English; expects headphone listings",
    ),
    _q(
        "B5-22", "language", "en", "wireless earbuds",
        OUTCOME_SUCCESS_WITH_RESULTS, "product",
        "English product query; expects earbud listings",
    ),
    _q(
        "B5-23", "language", "uk", "безпроводні навушники",
        OUTCOME_SUCCESS_WITH_RESULTS, "product",
        "Ukrainian product query; expects headphone listings",
    ),
    _q(
        "B5-24", "language", "uk", "зарядний пристрій для телефону",
        OUTCOME_SUCCESS_WITH_RESULTS, "product",
        "Ukrainian phone-charger query; expects charger listings",
    ),
    _q(
        "B5-25", "language", "ru", "led лампа usb",
        OUTCOME_SUCCESS_WITH_RESULTS, "product",
        "mixed-script (Latin + Cyrillic); expects lamp listings",
    ),

    # -- cross-category product queries ---------------------------------------
    _q(
        "B5-26", "electronics", "ru", "Samsung 990 Pro 2TB NVMe SSD",
        OUTCOME_SUCCESS_WITH_RESULTS, "product",
        "brand + model + spec; expects SSD listings",
    ),
    _q(
        "B5-27", "automotive", "ru", "видеорегистратор для Toyota Camry 70",
        OUTCOME_SUCCESS_WITH_RESULTS, "product",
        "car model compatibility; expects dashcam listings",
    ),
    _q(
        "B5-28", "tools", "ru", "шуруповерт Bosch GSR 18V аккумуляторный",
        OUTCOME_SUCCESS_WITH_RESULTS, "product",
        "brand + voltage; expects drill listings",
    ),
    _q(
        "B5-29", "pet", "ru", "ошейник для кота с GPS-трекером",
        OUTCOME_SUCCESS_WITH_RESULTS, "product",
        "pet compatibility + feature; expects collar listings",
    ),
    _q(
        "B5-30", "beauty", "ru", "фен Dyson Supersonic",
        OUTCOME_SUCCESS_WITH_RESULTS, "product",
        "brand + model; expects hair-dryer listings",
    ),

    # -- budget / constraint queries ------------------------------------------
    _q(
        "B5-31", "budget", "ru", "механическая клавиатура до 3000 руб",
        OUTCOME_SUCCESS_WITH_RESULTS, "product",
        "price ceiling; expects keyboard listings within budget",
    ),
    _q(
        "B5-32", "budget", "ru", "настольная лампа до 500 грн",
        OUTCOME_EMPTY_AFTER_CONSTRAINTS, "product",
        "tight UAH budget; may empty after explicit constraints",
    ),
    _q(
        "B5-33", "exclusion", "ru",
        "микроволновка без гриля и без функции разморозки",
        OUTCOME_SUCCESS_WITH_RESULTS, "product",
        "multi-exclusion; expects microwave listings without grill/defrost",
    ),
    _q(
        "B5-34", "vague", "ru", "стильная одежда",
        OUTCOME_SUCCESS_EMPTY_RAW, "ambiguous",
        "vague mood query; no concrete product type; expects empty raw",
    ),
    _q(
        "B5-35", "broad", "ru", "кабель usb type-c",
        OUTCOME_SUCCESS_WITH_RESULTS, "product",
        "broad commodity; expects USB-C cable listings",
    ),
]

DATASET_VERSION = "b5-v1"
DATASET_SIZE = len(B5_DATASET)

__all__ = ["B5_DATASET", "DATASET_VERSION", "DATASET_SIZE"]

