# -*- coding: utf-8 -*-
"""Stage 45 — isolated evaluation set for the comprehensive search-quality audit.

READ-ONLY: only DEFINES queries + expected interpretations. Never runs the bot,
never touches the network, never mutates production. Separate from the Stage 44
gold (preserved unchanged). Spans the bot's supported shopping domains and query
types. Only fields the CURRENT schema can represent are asserted; fields with
no model slot (intended_use, provenance) are recorded in notes only.
"""

from __future__ import annotations


def _q(qid, category, qtype, query, **kw):
    base = {
        "id": qid, "category": category, "qtype": qtype, "query": query,
        "product_type": None, "product_intent_applicable": False,
        "required_attributes": [], "excluded_product_types": [],
        "compatibility": [], "brand": None,
        "budget": {"max": None, "min": None, "currency": None},
        "must_stay_empty": [], "notes": "",
    }
    base.update(kw)
    return base


STAGE45_EVAL = [
    # -- electronics / computers ------------------------------------------------
    _q(101, "electronics", "brand_model", "Samsung 990 Pro 2TB NVMe SSD",
       product_type="ssd", product_intent_applicable=True,
       brand="Samsung", required_attributes=["2 ТБ", "NVMe"]),
    _q(102, "electronics", "spec_units", "монитор 27 дюймов 144 Гц",
       product_type="монитор", product_intent_applicable=True,
       required_attributes=["27 дюймов", "144 Гц"]),
    _q(103, "electronics", "budget", "механическая клавиатура до 3000 руб",
       product_type="клавиатура", product_intent_applicable=True,
       budget={"max": 3000, "currency": "RUB"}),
    _q(104, "electronics", "long",
       "ищу беспроводную мышь для ноутбука, тихую, с хорошей батареей "
       "и без подсветки",
       product_type="мышь", product_intent_applicable=True,
       required_attributes=["беспроводная"],
       excluded_product_types=["подсветки"], compatibility=["ноутбук"]),
    # -- home appliances --------------------------------------------------------
    _q(105, "appliances", "simple", "робот-пылесос",
       product_type="робот-пылесос", product_intent_applicable=True),
    _q(106, "appliances", "multi_exclusion",
       "микроволновка без гриля и без функции разморозки",
       product_type="микроволновая печь", product_intent_applicable=True,
       excluded_product_types=["гриль", "разморозка"]),
    _q(107, "appliances", "compat", "фильтр для воды под мойку",
       product_type="фильтр для воды", product_intent_applicable=True,
       compatibility=["мойка"]),
    # -- automotive -------------------------------------------------------------
    _q(108, "automotive", "compat_model", "видеорегистратор для Toyota Camry 70",
       product_type="видеорегистратор", product_intent_applicable=True,
       compatibility=["Toyota Camry 70"]),
    _q(109, "automotive", "spec_units",
       "автомобильный компрессор 12 В с манометром",
       product_type="компрессор", product_intent_applicable=True,
       required_attributes=["12 В", "манометр"]),
    _q(110, "automotive", "negation_absence",
       "дворники без каркаса бескаркасные 650 мм",
       product_type="дворники", product_intent_applicable=True,
       required_attributes=["650 мм"]),
    # -- tools / DIY ------------------------------------------------------------
    _q(111, "tools", "brand_model", "шуруповерт Bosch GSR 18V аккумуляторный",
       product_type="шуруповёрт", product_intent_applicable=True,
       brand="Bosch", required_attributes=["18V", "аккумуляторный"]),
    _q(112, "tools", "multi_req",
       "дрель-шуруповерт с двумя аккумуляторами и кейсом",
       product_type="дрель-шуруповёрт", product_intent_applicable=True,
       required_attributes=["два аккумулятора", "кейс"]),
    _q(113, "tools", "exclusion", "перфоратор без SDS-plus",
       product_type="перфоратор", product_intent_applicable=True,
       excluded_product_types=["SDS-plus"]),
    _q(113, "tools", "exclusion", "перфоратор без SDS-plus",
       product_type="перфоратор", product_intent_applicable=True,
       excluded_product_types=["SDS-plus"]),
    # -- clothing / accessories -------------------------------------------------
    _q(114, "clothing", "long",
       "зимняя куртка мужская водонепроницаемая с капюшоном размер L",
       product_type="куртка", product_intent_applicable=True,
       required_attributes=["водонепроницаемая", "капюшон", "L"]),
    _q(115, "clothing", "vague", "стильная одежда",
       must_stay_empty=["product_type"]),
    # -- sports / outdoor -------------------------------------------------------
    _q(116, "sports", "compat", "крепление для велосипеда под телефон",
       product_type="крепление", product_intent_applicable=True,
       compatibility=["велосипед", "телефон"]),
    _q(117, "sports", "spec_units", "гантели разборные 2x20 кг",
       product_type="гантели", product_intent_applicable=True,
       required_attributes=["20 кг"]),
    # -- pet --------------------------------------------------------------------
    _q(118, "pet", "compat_age", "ошейник для кота с GPS-трекером",
       product_type="ошейник", product_intent_applicable=True,
       compatibility=["кот"], required_attributes=["GPS"]),
    _q(119, "pet", "simple", "корм для собак",
       product_type="корм", product_intent_applicable=True,
       compatibility=["собаки"]),
    # -- garden -----------------------------------------------------------------
    _q(120, "garden", "spec_units",
       "шланг поливочный 20 метров с распылителем",
       product_type="шланг", product_intent_applicable=True,
       required_attributes=["20 метров"]),
    _q(121, "garden", "long",
       "хочу триммер для травы в саду, лёгкий, чтобы стричь у забора",
       product_type="триммер", product_intent_applicable=True),
    # -- beauty / personal care ------------------------------------------------
    _q(122, "beauty", "brand", "фен Dyson Supersonic",
       product_type="фен", product_intent_applicable=True, brand="Dyson"),
    _q(123, "beauty", "exclusion", "электрическая зубная щётка без звука",
       product_type="зубная щётка", product_intent_applicable=True,
       excluded_product_types=["звука"]),
    # -- children ---------------------------------------------------------------
    _q(124, "children", "compat_age", "конструктор для мальчика 5 лет",
       product_type="конструктор", product_intent_applicable=True,
       compatibility=["мальчик"], required_attributes=["5 лет"]),
    _q(125, "children", "simple", "детская коляска",
       product_type="коляска", product_intent_applicable=True),
    # -- mixed-script / translit / synonym -------------------------------------
    _q(126, "language", "translit", "powerbank 20000",
       product_type="power bank", product_intent_applicable=True,
       required_attributes=["20000"]),
    _q(127, "language", "mixed", "led лампа usb",
       product_type="лампа", product_intent_applicable=True,
       required_attributes=["led", "usb"]),
    _q(128, "language", "synonym", "наушники беспроводные блютуз",
       product_type="наушники", product_intent_applicable=True,
       required_attributes=["беспроводные", "bluetooth"]),
    # -- insufficient / clarification ------------------------------------------
    _q(129, "insufficient", "vague", "устройство",
       must_stay_empty=["product_type"]),
    _q(130, "insufficient", "vague", "что-нибудь для машины",
       must_stay_empty=["product_type"]),
    # -- duplicate-risk broad query --------------------------------------------
    _q(131, "electronics", "broad", "кабель usb type-c",
       product_type="кабель", product_intent_applicable=True,
       required_attributes=["usb", "type-c"]),
]

