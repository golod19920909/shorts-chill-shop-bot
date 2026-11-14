"""Additional blind cases for the 3.1 vs 3.5 Flash-Lite experiment.

These are one-turn product-intent cases across RU/UK/EN and unrelated product
families. They contain no synthetic product listings; candidate selection is
benchmarked separately only from authentic AliExpress snapshots.
"""

from __future__ import annotations

from tests.eval_consultant_dialogue_dataset import _context, _turn

OPTIMIZATION_DATASET_VERSION = "consultant-optimization-v1"

_ROWS = [
    ("O001", "ru", "electronics", "Найди беспроводной дверной звонок", "дверной звонок"),
    ("O002", "ru", "tools", "Подбери лазерный уровень", "лазерный уровень"),
    ("O003", "ru", "lighting", "Найди настольную лампу", "настольная лампа"),
    ("O004", "ru", "home", "Ищу ручной отпариватель", "отпариватель"),
    ("O005", "ru", "automotive", "Подбери автомобильный компрессор", "компрессор"),
    ("O006", "ru", "automotive", "Найди видеорегистратор", "видеорегистратор"),
    ("O007", "ru", "cycling", "Нужен велосипедный фонарь", "велосипедный фонарь"),
    ("O008", "ru", "fishing", "Подбери рыболовную катушку", "рыболовная катушка"),
    ("O009", "ru", "outdoors", "Найди туристическую горелку", "туристическая горелка"),
    ("O010", "ru", "outdoors", "Ищу термос", "термос"),
    ("O011", "ru", "tools", "Подбери паяльную станцию", "паяльная станция"),
    ("O012", "ru", "personal_care", "Найди электрическую зубную щётку", "зубная щётка"),
    ("O013", "ru", "home", "Нужен увлажнитель воздуха", "увлажнитель воздуха"),
    ("O014", "ru", "home", "Подбери робот-пылесос", "робот-пылесос"),
    ("O015", "ru", "garden", "Найди садовый секатор", "секатор"),
    ("O016", "ru", "garden", "Ищу кормушку для птиц", "кормушка"),
    ("O017", "ru", "toys", "Подбери детский конструктор", "конструктор"),
    ("O018", "ru", "games", "Найди настольную игру", "настольная игра"),
    ("O019", "ru", "wearables", "Ищу фитнес-браслет", "фитнес-браслет"),
    ("O020", "ru", "audio", "Подбери петличный микрофон", "микрофон"),
    ("O021", "ru", "lighting", "Найди светодиодную ленту", "светодиодная лента"),
    ("O022", "uk", "lighting", "Знайди акумуляторний ліхтар", "ліхтар"),
    ("O023", "uk", "garden", "Потрібен дощомір", "дощомір"),
    ("O024", "uk", "accessories", "Підбери чохол для планшета", "чохол"),
    ("O025", "uk", "computers", "Знайди підставку для ноутбука", "підставка"),
    ("O026", "uk", "kitchen", "Шукаю електричну м'ясорубку", "м'ясорубка"),
    ("O027", "uk", "automotive", "Підбери автомобільний пилосос", "пилосос"),
    ("O028", "uk", "video", "Знайди портативний проєктор", "проєктор"),
    ("O029", "uk", "fitness", "Потрібен тренажер для преса", "тренажер"),
    ("O030", "uk", "garden", "Шукаю садовий обприскувач", "обприскувач"),
    ("O031", "uk", "kitchen", "Підбери кухонні ваги", "ваги"),
    ("O032", "uk", "lighting", "Знайди дитячий нічник", "нічник"),
    ("O033", "uk", "automotive", "Потрібен мінікомпресор", "компресор"),
    ("O034", "uk", "outdoors", "Підбери туристичний рюкзак", "рюкзак"),
    ("O035", "uk", "home", "Знайди настільний вентилятор", "вентилятор"),
    ("O036", "uk", "smart_home", "Шукаю розумну розетку", "розетка"),
    ("O037", "uk", "computers", "Підбери ігрову клавіатуру", "клавіатура"),
    ("O038", "uk", "security", "Знайди бездротову камеру", "камера"),
    ("O039", "uk", "automotive", "Потрібен тримач для телефону в авто", "тримач"),
    ("O040", "uk", "tools", "Підбери набір викруток", "набір викруток"),
    ("O041", "uk", "outdoors", "Знайди термокружку", "термокружка"),
    ("O042", "en", "storage", "Find a portable SSD", "portable SSD"),
    ("O043", "en", "computers", "Find a mechanical keyboard", "mechanical keyboard"),
    ("O044", "en", "security", "I need a wireless security camera", "security camera"),
    ("O045", "en", "smart_home", "Find a smart plug", "smart plug"),
    ("O046", "en", "tools", "Help me find a cordless drill", "cordless drill"),
    ("O047", "en", "tools", "Find a digital caliper", "digital caliper"),
    ("O048", "en", "automotive", "I need a portable air compressor", "air compressor"),
    ("O049", "en", "outdoors", "Find a camping lantern", "camping lantern"),
    ("O050", "en", "fishing", "Find a fishing reel", "fishing reel"),
    ("O051", "en", "pets", "I need a dog water fountain", "dog water fountain"),
    ("O052", "en", "pets", "Find an automatic pet feeder", "automatic pet feeder"),
    ("O053", "en", "garden", "Find solar garden lights", "solar garden lights"),
    ("O054", "en", "kitchen", "I need an espresso scale", "espresso scale"),
    ("O055", "en", "kitchen", "Find an electric milk frother", "milk frother"),
    ("O056", "en", "video", "Find a mini projector", "mini projector"),
    ("O057", "en", "cycling", "I need a bike phone mount", "phone mount"),
    ("O058", "en", "audio", "Find a USB microphone", "USB microphone"),
    ("O059", "en", "electronics", "Find a wireless doorbell", "wireless doorbell"),
    ("O060", "en", "office", "I need a portable label printer", "label printer"),
    ("O061", "en", "kitchen", "Find a digital kitchen thermometer", "kitchen thermometer"),
]

OPTIMIZATION_CASES = [
    {
        "id": case_id,
        "scenario": f"single_turn_{family}",
        "language": language,
        "turns": [
            _turn(
                user_text,
                "new_search",
                context=_context(),
                product_type=product_type,
                retrieval=True,
                note="Identify the explicitly requested product head; no prior context or hidden constraints.",
            )
        ],
    }
    for case_id, language, family, user_text, product_type in _ROWS
]

__all__ = ["OPTIMIZATION_DATASET_VERSION", "OPTIMIZATION_CASES"]
