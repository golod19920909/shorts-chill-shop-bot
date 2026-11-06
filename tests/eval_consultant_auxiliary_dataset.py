"""Artificial auxiliary-operation corpus for the Option C consultant.

These cases test only conversational clarification and no-results wording.
They are not marketplace product evidence and must never be used to score
AliExpress candidate selection, ranking, compatibility, or factual product claims.
"""

from __future__ import annotations


DATASET_VERSION = "consultant-auxiliary-v1"

CHAT_ACTION_CASES = [
    {"id": "C01", "language": "ru", "text": "Для машины", "expected_action": "clarify"},
    {"id": "C02", "language": "uk", "text": "Для подорожей", "expected_action": "clarify"},
    {"id": "C03", "language": "en", "text": "For home", "expected_action": "clarify"},
    {"id": "C04", "language": "ru", "text": "Беспроводные наушники", "expected_action": "search"},
    {"id": "C05", "language": "uk", "text": "Павербанк 65 Вт", "expected_action": "search"},
    {"id": "C06", "language": "en", "text": "USB-C wall charger", "expected_action": "search"},
    {"id": "C07", "language": "ru", "text": "Покороче", "expected_action": "clarify"},
    {"id": "C08", "language": "ru", "text": "На 12 вольт", "expected_action": "clarify"},
    {"id": "C09", "language": "uk", "text": "Для дачі", "expected_action": "clarify"},
    {"id": "C10", "language": "en", "text": "For camping", "expected_action": "clarify"},
    {"id": "C11", "language": "ru", "text": "Сковорода 28 см", "expected_action": "search"},
    {"id": "C12", "language": "en", "text": "Noise-cancelling headphones", "expected_action": "search"},
    {"id": "C13", "language": "uk", "text": "Щось потужніше", "expected_action": "clarify"},
    {"id": "C14", "language": "en", "text": "A compact desk lamp", "expected_action": "search"},
]

NO_RESULTS_CASES = [
    {
        "id": "N01", "language": "ru",
        "user_text": "Найди компактный очиститель воздуха для спальни до 3000 грн",
        "active_slots": [
            {"kind": "type", "value": "очиститель воздуха"},
            {"kind": "use_case", "value": "для спальни"},
            {"kind": "budget", "value": "до 3000 грн"},
        ],
        "reason": "no_eligible",
    },
    {
        "id": "N02", "language": "uk",
        "user_text": "Потрібен павербанк для ноутбука з USB-C Power Delivery 100W",
        "active_slots": [
            {"kind": "type", "value": "павербанк"},
            {"kind": "compatibility", "value": "для ноутбука"},
            {"kind": "feature", "value": "USB-C Power Delivery 100W"},
        ],
        "reason": "no_eligible",
    },
    {
        "id": "N03", "language": "en",
        "user_text": "Find a quiet dehumidifier for a small bedroom",
        "active_slots": [
            {"kind": "type", "value": "dehumidifier"},
            {"kind": "use_case", "value": "small bedroom"},
            {"kind": "feature", "value": "quiet"},
        ],
        "reason": "no_candidates",
    },
    {
        "id": "N04", "language": "ru",
        "user_text": "Нужен внешний SSD на 2 ТБ с USB-C",
        "active_slots": [
            {"kind": "type", "value": "внешний SSD"},
            {"kind": "feature", "value": "2 ТБ"},
            {"kind": "feature", "value": "USB-C"},
        ],
        "reason": "no_eligible",
    },
    {
        "id": "N05", "language": "uk",
        "user_text": "Шукаю настільну лампу з акумулятором і регулюванням яскравості",
        "active_slots": [
            {"kind": "type", "value": "настільна лампа"},
            {"kind": "feature", "value": "з акумулятором"},
            {"kind": "feature", "value": "регулювання яскравості"},
        ],
        "reason": "no_candidates",
    },
    {
        "id": "N06", "language": "en",
        "user_text": "I need a waterproof Bluetooth speaker with a replaceable battery",
        "active_slots": [
            {"kind": "type", "value": "Bluetooth speaker"},
            {"kind": "feature", "value": "waterproof"},
            {"kind": "feature", "value": "replaceable battery"},
        ],
        "reason": "no_eligible",
    },
    {
        "id": "N07", "language": "ru",
        "user_text": "Подбери автомобильный компрессор с питанием от 12 В и автоотключением",
        "active_slots": [
            {"kind": "type", "value": "автомобильный компрессор"},
            {"kind": "compatibility", "value": "12 В"},
            {"kind": "feature", "value": "автоотключение"},
        ],
        "reason": "no_eligible",
    },
    {
        "id": "N08", "language": "uk",
        "user_text": "Потрібен рюкзак для ноутбука 16 дюймів без шкіряних деталей",
        "active_slots": [
            {"kind": "type", "value": "рюкзак"},
            {"kind": "compatibility", "value": "ноутбук 16 дюймів"},
            {"kind": "exclusion", "value": "без шкіряних деталей"},
        ],
        "reason": "no_candidates",
    },
    {
        "id": "N09", "language": "en",
        "user_text": "Find a cordless drill with one battery and a metal chuck",
        "active_slots": [
            {"kind": "type", "value": "cordless drill"},
            {"kind": "feature", "value": "one battery"},
            {"kind": "feature", "value": "metal chuck"},
        ],
        "reason": "no_eligible",
    },
    {
        "id": "N10", "language": "ru",
        "user_text": "Ищу настенный держатель для монитора 34 дюйма с креплением VESA",
        "active_slots": [
            {"kind": "type", "value": "держатель для монитора"},
            {"kind": "compatibility", "value": "34 дюйма"},
            {"kind": "feature", "value": "VESA"},
        ],
        "reason": "no_candidates",
    },
    {
        "id": "N11", "language": "uk",
        "user_text": "Потрібен зарядний пристрій для двох акумуляторів одночасно",
        "active_slots": [
            {"kind": "type", "value": "зарядний пристрій"},
            {"kind": "feature", "value": "для двох акумуляторів одночасно"},
        ],
        "reason": "no_eligible",
    },
    {
        "id": "N12", "language": "en",
        "user_text": "I need a compact travel kettle with a 110–240V input",
        "active_slots": [
            {"kind": "type", "value": "travel kettle"},
            {"kind": "feature", "value": "compact"},
            {"kind": "compatibility", "value": "110–240V input"},
        ],
        "reason": "no_candidates",
    },
]


def validate_dataset() -> None:
    """Fail fast on duplicate IDs, invalid languages or invalid actions."""
    ids = [case["id"] for case in CHAT_ACTION_CASES + NO_RESULTS_CASES]
    if len(ids) != len(set(ids)):
        raise ValueError("Auxiliary evaluation case IDs must be unique")
    if any(case["language"] not in {"ru", "uk", "en"} for case in CHAT_ACTION_CASES + NO_RESULTS_CASES):
        raise ValueError("Unsupported auxiliary evaluation language")
    if any(case["expected_action"] not in {"search", "clarify"} for case in CHAT_ACTION_CASES):
        raise ValueError("Invalid expected chat action")
    if any(not case["active_slots"] or not case["reason"] for case in NO_RESULTS_CASES):
        raise ValueError("No-results cases require active slots and a reason")
