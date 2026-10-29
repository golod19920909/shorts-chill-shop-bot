# PROJECT OVERVIEW — Telegram Shop Bot

> Документ описывает архитектуру, бизнес-логику и интеграции бота без изменений
> в исходном коде. Составлен по фактическому состоянию репозитория
> (`c:\shorts-chill-shop-bot`). Тестовый набор: 39 модулей, 864 теста — проходят.

## 1. Что это

Телеграм-бот шоппинг-помощник на русском языке: поиск товаров на AliExpress
через официальный Affiliate API, лента «✨ Находки» (G24: широкая выдача
AliExpress без keywords и без sort) и «горячие скидки», поиск по фотографии,
избранное, автоматический трекинг цен, админ-панель с диагностикой. Ключевой
принцип проекта — **честность данных**:
бот никогда не выдумывает товары, цены, ссылки, скидки или «AI-результаты».
Всё, что видит пользователь, пришло из реального API маркетплейса; Gemini
используется только там, где это документировано и подтверждено, и всегда с
детерминированными фолбэками.

## 2. Технологический стек

| Компонент | Технология | Примечание |
|---|---|---|
| Бот-фреймворк | `python-telegram-bot` v20.x (API v20.13) | async, JobQueue для фоновых задач |
| Язык / рантайм | Python 3.12, asyncio | `.venv/Scripts/python.exe` |
| Модели данных | `pydantic` v2 (`BaseModel`, `Field`) | `models/product.py`, `models/search_request.py` |
| База данных | SQLite из stdlib (`sqlite3`) | собственный async-слой в `database/database.py`, без ORM |
| HTTP-клиент | `httpx` | один общий `httpx.AsyncClient` на приложение |
| AI | `google-genai` (Gemini API) | все вызовы — в `services/ai_service.py` |
| Тесты | стандартный `unittest` | 864 теста, полностью офлайн (заглушки вместо сети) |

## 3. Слоистая архитектура

```
Telegram-пользователь
        │
main.py ──► bot/__init__.py  (сборка приложения, DI через bot_data)
        │
handlers/   — UI и пользовательские сценарии
  start.py        /start /help, регистрация пользователя
  search.py       свободный поиск по тексту + все действия на карточке
  discovery.py    «✨ Находки» (G24-фид), категории, «горячие скидки», фото
  favorites.py    избранное; settings.py — регион/валюта и уведомления
  history.py      история поиска; legal.py — /about /privacy /terms
  admin.py        админ-панель (13 секций, гейт по ADMIN_IDS)
  common.py       DI-доступ, сессионный кэш, карточки, кuration-гейт
        │
services/   — бизнес-логика и интеграции
  search_service.py     конвейер поиска (retrieval → gate → curation → cap)
  ai_service.py         ВСЕ Gemini-вызовы + regex-фолбэк парсинга запроса
  curation_service.py   гейт курации + журнал отбраковок
  conversation.py       уточнение запроса (слияние с предыдущим)
  product_identity.py   идентичность/дедуп товара
  relevance.py          финальный relevance-gate (товар ↔ запрос)
  price_tracker.py      фоновый трекинг цен + уведомления
  notifications.py      новости каталога и напоминания (JobQueue)
  region.py             регионы/валюты; analytics.py — событийная аналитика
  marketplaces/         base.py (абстракция), aliexpress.py (провайдер),
                        aliexpress_client.py (TOP-клиент: шлюз, подпись, ретраи)
curation/   — независимый конвейер ленты: pipeline.py (5 стадий),
              integration.py (CurationGate, modes/scopes, журнал причин),
              history.py (HistoryStore поверх user_view_history)
database/   — SQLite: схема, запросы, индексы (models.py + database.py)
config/     — settings.py (env), catalog.py (категории/валюты),
              promo.py, hot_discounts.py
keyboards/  — inline-клавиатуры;  utils/ — форматирование, логирование
tests/      — юнит-тесты; smoke_ux_flows.py, smoke_aliexpress.py
```

Сборка зависимостей целиком в `bot/__init__.py`: один `httpx.AsyncClient`
переиспользуют Gemini- и AliExpress-клиенты; `Settings`, `Database`,
`AIService`, `SearchService`, `CurationGate`, провайдеры и `HistoryStore`
кладутся в `application.bot_data`, откуда обработчики достают их через
`handlers/common.py` (`get_database`, `get_search_service`, …).
Ключевая инвариантная нота DI: «ни один товар не рождается в Gemini» — AI
лишь читает запрос/фото и ранжирует уже найденное маркетплейсом.


## 4. Конфигурация (env / `.env`)

Загружается `config/settings.load_settings()`; все числовые значения
клампятся (`_get_int` с min/max), поэтому опечатка в `.env` не может
превратить один запрос пользователя в краулинг маркетплейса.

| Переменная | Дефолт | Назначение |
|---|---|---|
| `BOT_TOKEN` | — (обязательна) | токен Telegram-бота |
| `ALIEXPRESS_APP_KEY` / `ALIEXPRESS_APP_SECRET` | пусто | ключи Affiliate API; без них провайдер честно возвращает пустые результаты, бот работает |
| `ALIEXPRESS_GATEWAY` | `https://api-sg.aliexpress.com/sync` | сингапурский affiliate-шлюз (legacy `eco.taobao.com/router/rest` отвергает этот ключ с `isv.appkey-not-exists`) |
| `GEMINI_API_KEY` | пусто | без ключа `AIService.is_available=False` и включаются regex-фолбэки |
| `GEMINI_FAST_MODEL` | `gemini-3.8-flash` | роль fast: парсинг запросов, отбор и ранжирование кандидатов, refinement |
| `GEMINI_SMART_MODEL` | `gemini-3.8-flash` | роль smart: анализ товара, свободные вопросы |
| `GEMINI_VISION_MODEL` | `gemini-3.8-flash` | роль vision: чтение фотографий |
| `GEMINI_FALLBACK_MODEL` | `gemini-3.1-flash-lite` | пробуется один раз при «модель недоступна» (не при quota — она account-wide) |
| `ADMIN_IDS` | пусто | список id через запятую; пусто = доступ не дан никому |
| `SEARCH_MAX_PAGES` | 3 (1–10) | глубина text-retrieval на один запрос (это реальные API-вызовы) |
| `PHOTO_SEARCH_PAGES` | 2 (1–5) | сколько документированных страниц одной фото-гипотезы запрашивать |
| `PHOTO_SEARCH_HYPOTHESES` | 4 (1–6) | сколько различных grounded-гипотез может стоить одно фото |
| `TRACKING_INTERVAL_MINUTES` | 1440 (0 = выкл.) | период job-а трекинга цен |
| `DATABASE_PATH` | `database/shop_bot.db` | файл SQLite (относительный путь резолвится от корня проекта) |
| `LOG_LEVEL` и др. | INFO | файл-лог + кольцевой буфер (`utils.logging.get_recent_logs`) для админ-панели |

Промокоды (`config/promo.py`) и ключевые слова «горячих скидок»
(`config/hot_discounts.py`) — **константы в коде, не env**: промокоды
уникальны для кампании и меняются вместе с кодом, а keywords должны
совпадать с лексикой ранжированного сида popularity-фида.

Каталог (`config/catalog.py`): 12 категорий (электроника, дом, авто,
компьютеры, игры, одежда, красота, инструменты, спорт, животные, дети,
сад) с кодом, заголовком и search-подсказкой на английском; «подарки»
удалены продуктовым решением и защищены от возврата `REMOVED_CATEGORY_CODES`.
Валюты пользователя: `UAH, USD, RUB, BYN, EUR, KZT, UZS, PLN, GBP` —
это только передаваемая провайдеру preference, бот никогда не конвертирует цены.

## 5. База данных (SQLite)

Схема идемпотентна (`CREATE TABLE IF NOT EXISTS`), таблицы и индексы
создаются одним блоком в `Database.initialize`. `product_json` хранит
полный снимок `Product`, чтобы карточка избранного/трекинга воспроизводилась
без повторного обращения к API.

| Таблица | Назначение | Ключевые ограничения |
|---|---|---|
| `users` | пользователи: `telegram_id`, `username`, `country`, `currency`, `onboarded`, `notify_price_changes` | `telegram_id UNIQUE` |
| `search_history` | история поиска: `original_query` + сериализованный `SearchRequest` (`search_request_json`) | FK → users, `ON DELETE CASCADE`; индекс `(user_id, id DESC)` |
| `favorites` | избранное: `marketplace`, `product_id`, `product_json` | `UNIQUE (user_id, marketplace, product_id)` |
| `saved_products` | сохранённые товары (с заметкой) | `UNIQUE (user_id, marketplace, product_id)` |
| `tracked_products` | трекинг цен: `product_url`, `initial_price`, `last_checked_price`, `currency`, `is_active` | `UNIQUE (user_id, marketplace, product_id)`; индекс `(is_active, user_id)` для job-а |
| `price_history` | история цен: `price`, `shipping_cost`, `currency`, `checked_at` | FK → tracked_products CASCADE; индекс `(tracked_product_id, id DESC)` |
| `analytics_events` | события: `event_type`, `marketplace`, `product_id`, `is_affiliate`, `metadata_json` | FK user `ON DELETE SET NULL`; индексы по `created_at`, `(event_type, created_at)`, `(user_id, id DESC)` |
| `popular_shown` | какие карточки уже показал пользователь (scope `flow`: `feed` / `search`) — история «✨ Находки» и поиск раздельно | индекс `(user_id, id DESC)` |
| `popular_state` | курсор «✨ Находки» на пользователя: следующий `page_no` AliExpress (колонка `step`) | `user_id PRIMARY KEY` |
| `user_view_history` | персонизированная новизна для курации (scope-разделение ниже) | `UNIQUE (user_id, product_id)`; индекс `(user_id, id DESC)` |

Разделение потоков (`FLOW_SEARCH` / `FLOW_FEED`): ordinary-поиск и
лента «✨ Находки» ведут учёт показанного **раздельно**, чтобы transient-
состояние одного экрана не решало, что показывать на другом (задокументированный
производственный баг: обычный поиск мог показать экран «лента исчерпана»).

Слой доступа `database/database.py` — тонкий async-обёртка над stdlib
`sqlite3` (никакой ORM), все запросы параметризованы.


## 6. Пользовательские экраны и сценарии

Главное меню (inline, `keyboards/main.py`, callback-префикс `menu:`):
поиск, поиск по фото, «✨ Находки», «🔥 Горячие скидки», промокоды
(`menu:pcodes`), категории, избранное, трекинг, валюта, настройки,
уведомления, обучение/туториал, помощь. При первом `/start` показывается
многоэкранное обучение (`onb:<step>` → `onb:done`, флаг `users.onboarded`).

### 6.1. Поиск по тексту (один экран, без списков)

```
текст пользователя → AIService.parse_query (fast-модель; regex-фолбэк)
  → services.conversation.resolve_request  (уточнение = слияние с прошлым
    запросом через MERGE_PROMPT; last-запрос в user_data[search_conversation])
  → SearchService.find_products            (см. §8)
  → ПЕРВАЯ карточка товара одним сообщением
```

Списка результатов намеренно нет: ни «Найдено N товаров», ни пагинации.
Единственное сообщение карточки **редактируется на месте** кнопками
«➡️ Следующий» / «◀️ Назад». Весь видимый набор кэшируется в памяти
(`handlers/common.remember_outcome`): короткий `session_id = token_hex(3)`,
максимум 5 последних сессий на пользователя; callback'и носят только
`(session_id, index)` — реальные id/URL товаров не едут через callback_data.

Кнопки карточки (`keyboards/products.py`):

| Префикс | Формат | Действие |
|---|---|---|
| `pnav` | `pnav:<sid>:<idx>` | перейти к карточке idx той же сессии (edit in place; для ленты «✨ Находки» — claim нового товара с проверкой истории) |
| inline-URL | «🛒 Купить» | `product.best_url` = `affiliate_url or product_url`, событие `buy_clicked`/`affiliate_clicked` |
| `pfav` / `ptun`/`ptrack` | `:<sid>:<idx>` | избранное / трекинг прямо с карточки |
| `pai`, `paia` | `paia:<action>:<sid>:<idx>` | AI-разбор товара (smart-модель по реальным полям) |
| `fdel` | `fdel:<marketplace>:<product_id>` | удалить из избранного |
| `tstop` | `tstop:<tracked_id>` | остановить трекинг |

Пустой результат — всегда одна короткая честная фраза
«ничего не найдено, измените запрос»; причины (ошибки API, отброшенные
кандидаты) уходят только в лог.

### 6.2. Категории и «✨ Находки»

* Категории: `cat:<code>` из `config/catalog.py` → тот же поисковый конвейер
  с anchored-запросом категории, далее обычный фиид карточек.
* «✨ Находки» — **лента G24**: `SearchService.find_feed_page` отдаёт
  ровно одну документированную страницу официального
  `aliexpress.affiliate.product.query` БЕЗ `keywords` и БЕЗ `sort`
  (порядок и состав определяет сам AliExpress; метод обслуживает и поиск,
  и ленту — `supports_feed = True`). Техническая очистка страницы:
  нормализация → дедуп по `product_identity` → требуются картинка и
  affiliate-ссылка → исключение уже показанного этому пользователю
  (scope `FLOW_FEED`, retention 1000 строк). Никакого ranking/quality/
  diversity/AI-отбора на этом пути нет — это закреплено контрактными
  тестами `tests/test_promotions.py`. `handlers/discovery.show_feed`
  собирает из страниц до `FEED_OPEN_SIZE` (20) карточек за не более
  чем `FEED_MAX_PAGES_PER_OPEN` (2) страницы, причём вторая страница
  спрашивается только если первой не хватило; собственные карточки
  хранятся в RAM-pool сессии (`handlers/common.remember_outcome`) и
  выдаются навигацией без повторных API-запросов. Курсор (`page_no`)
  хранится в `popular_state` (колонка `step`, RAM + SQLite), показанное —
  в `popular_shown` (scope `FLOW_FEED`): `next = last_attempted + 1`,
  переживает рестарт процесса, кнопка `feed:reset` (легаси `pop:reset`)
  сбрасывает показанное, но НЕ курсор. Когда страницы не дают ничего
  нового — честный экран «показывать те же товары повторно бот не будет»
  с кнопкой «🔄 Начать заново», а не бесконечный цикл.
* «🔥 Горячие скидки» (`disc:hot`-секции из `config/hot_discounts.py`):
  подборки реальных товаров с реальным `discount_percent` из API по
  тематическим ключевым словам; порог и секции — код-константы.
* Промокоды (`menu:pcodes`, `config/promo.py`) — статические витрины
  промокодов, бот показывает только то, что заведено в конфиге.

### 6.3. Избранное и трекинг

Избранное — CRUD поверх таблицы `favorites` (`fdel` удаляет, список рендерит
карточки-снипшоты). Трекинг — подписка на `tracked_products`; ручные
`/track`-проверки работают всегда, автоматика — job из §11. Уведомления
уважают `users.notify_price_changes`.


## 7. Конвейер поиска (`services/search_service.py`)

Единая точка `SearchService.find_products(request, exclude, broaden)`,
через которую идут текст, фото и категории (лента «✨ Находки» ходит
отдельной точкой `find_feed_page`). Возвращает
`SearchOutcome` с честной бухгалтерией: `products_before_filtering`,
`products_dropped_as_irrelevant`, `products_hidden_as_seen`,
`searched/failed/unavailable_marketplaces`.

1. **Retrieval.** Провайдер `aliexpress.affiliate.product.query`:
   `keywords` (≤100 симв.), `page_no`, `page_size` (клампится в документированный
   диапазон 1..50), `target_language`, `target_currency` (из предпочтения
   пользователя, если API его поддерживает). Ценовые рамки API НЕ шлются —
   единицы `min_sale_price`/`max_sale_price` документированы неоднозначно,
   поэтому ценовые фильтры применяются локально к реальным ценам.
2. **Углубление.** Если релевантный пул тонок, тот же запрос повторяется на
   следующих документированных страницах/вариантах — не более
   `SEARCH_MAX_PAGES` реальных вызовов. Когда у AI-сервиса есть селектор
   (`select_feed_candidates`), целевой размер пула — `RAW_POOL_TARGET = 80`
   сырых позиций: модели нужно из чего ВЫБИРАТЬ, поэтому пул собирается
   страницами (1 страница = 50 позиций, документированный максимум). Без
   доступного AI цель — прежний browse-cap (30), и число вызовов не меняется.
3. **Relevance-gate** (`services/relevance.py`): кандидат обязан соответство-
   вать концепту запроса; совпадение одного случайного слова не спасает.
4. **Дедуп и новизна:** идентичность по `product_identity`, скрытие
   показанного (per-flow scope из §5).
5. **AI-отбор (§3).** Глубокий пул уходит в `select_feed_candidates`: модель
   выбрасывает мусор по общим строгим правилам (`STRICT_FILTER_RULES` —
   рейтинг ниже 4.5, не тот товар/аксессуары, аномально дешёвый спам, дубли) и
   возвращает не более `TOP_CANDIDATES = 20` лучших. Ответ — ВЫБОР, а не
   перестановка: id, которых в нём нет, до карточек не доходят
   (`feed_curation.apply_selection`), cap 20 проверяется кодом, а не промптом.
   Потоки, собирающие пул сами (фото-гипотезы), просят отбор ОДИН раз на
   собранном пуле (`SearchService.apply_feed_selection`).
6. **Курация** — §9, затем cap: 20 после AI-отбора, иначе ~30 товаров
   (размер просматриваемого пула). Любой сбой AI возвращает детерминированный
   список, поэтому лента не может опустеть из-за модели.

`SortPreference` (`relevance / price_asc / price_desc / rating_desc /
orders_desc / total_cost_asc / popularity_desc`) реализованы детерминированно;
`min_rating` честно помечается неприменимым, когда API не отдаёт рейтинг
(`rating_filter_applicable`).

## 8. Поиск по фотографии (Vision AI)

Нативный `aliexpress.affiliate.image.search` **неиспользуем**: метод
требует обязательные `image_file_bytes` (byte[], ≤100 КБ — шлюз не принимает
ни в одном транспонде) и `app_signature` (платформенная возможность, которой
у этого приложения нет). Это проверено live: на том же шлюзе с теми же
ключами control-метод `product.query` отвечает нормально. Поэтому честный
фолбэк (`handlers/discovery.photo_message`):

```
фото Telegram (лимит 5 МБ) → скачивание в память
→ AIService.analyze_image (VISION-модель, IMAGE_ANALYSIS_PROMPT)
   structured JSON: is_product, product_type, brand, model, product_name,
   visible_text, color, material, form_factor, ... — только то, что реально
   видно/читается на фото; поля вне белого списка отбрасываются, ответ без
   product_type/product_name/visible_text бракуется (_validated_image_analysis)
→ photo_hypotheses(): ТИРОВАННЫЕ grounded-гипотезы (точное видимое имя →
   бренд+тип → весь тип → CORE-слова типа → категория типа → тип+атрибут →
   текст с упаковки → бренд+модель); переформулировки уже заданного вопроса
   отбрасываются (порог NEAR_DUPLICATE_THRESHOLD), не более
   PHOTO_SEARCH_HYPOTHESES (дефолт 4, потолок 6)
→ для каждой гипотезы — обычный конвейер (§7) с keywords-валидацией по
   концепту фото (photo_concept_keywords), broaden=False; глубина —
   PHOTO_SEARCH_PAGES (дефолт 2), и только для фраз, давших ответ на
   странице 1; остановка при 30 валидных товарах
→ merge + dedup + relevance-rank → стандартный фиид карточек
```

Сгенерированные запросы пользователю не показываются; Gemini никогда не
называет цены/ссылки — он только «читает картинку». Без Gemini — короткий
честный ответ «не удалось определить товар» (техническая причина — только в
лог). Legacy-режим `IMAGE_QUERY_PROMPT` (одна короткая фраза) остаётся для
интеграций без structured-анализа, но никогда не выполняется вместе с ним.

## 9. Курация ленты (`curation/`)

`CurationGate` (`integration.py`) — единственная точка входа: валидные
`mode` (`deals`/`popular`/`search`) и `scope`, журнал причин для каждой
отбраковки (админ может посмотреть, ЧТО и ПОЧЕМУ отфильтровано).
`pipeline.py` — пять чистых стадий над реальными полями:

1. **Нормализация** — извлечение чисел/сигналов только из реальных полей;
   отсутствующий сигнал = `-1.0`, то есть всегда проигрывает настоящему.
2. **Quality / near-dup fold** — «переписанные близнецы» одного товара
   («RGB Механическая клавиатура…» / «Механическая клавиатура RGB…»)
   схлопываются в одно лучшее предложение семьи по `quality_key`:
   заказы → рейтинг → % положительных (рыночные сигналы, не выдумка).
3. **HistoryFilter (boredom)** — `user_view_history` как сигнал НОВИЗНЫ, а
   не блэклист: показанное уходит в хвост, если невиданного не хватает на
   экран; фид никогда не обнуляется историей.
4. **ModeRanker** — упорядочивание в одном смысле за раз: `deals` —
   сначала наибольшая реальная скидка (вычисляется из price/old_price;
   при отсутствующем price — `None`, не вымысел), затем заказы; `popular` —
   заказы/рейтинг; `search` — relevance, сигналы только «при прочих равных».
5. **DiversityFilter** — один тип товара не занимает блок карточек
   («не больше двух клавиатур на экран»); тип = множество токенов заголовка
   без слов-паразитов, порядок слов не влияет.


## 10. Интеграция с AliExpress Affiliate API

### 10.1. Транспорт (`services/marketplaces/aliexpress_client.py`)

Клиент TOP-протокола, verified против живой платформы:

* шлюз по умолчанию — сингапурский `https://api-sg.aliexpress.com/sync`
  (legacy `eco.taobao.com/router/rest` отвергает ключ с `isv.appkey-not-exists`,
  код 29; константа оставлена для диагностики/тестов);
* публичные параметры: `method`, `app_key`, `timestamp`
  (`yyyy-MM-dd HH:mm:ss` в GMT+8, допуск перекоса часов ≤10 минут),
  `format`, `v`, `sign_method`, `sign`;
* подпись: `md5` → `MD5(secret + sorted(k+v...) + secret)` или
  `hmac` → `HMAC_MD5(sorted(k+v...), secret)`;
* ретраи на сетевые ошибки, метрики `calls_ok/calls_err/retries_spent/empty_results`
  (видны в админ-панели); в ошибки никогда не попадают секрет и подпись.

### 10.2. Провайдер (`services/marketplaces/aliexpress.py`)

* Поиск и лента «✨ Находки» — один и тот же
  `aliexpress.affiliate.product.query`: поиск задаёт `keywords` (и
  опционально `sort` по явному пользовательскому предпочтению), лента G24 —
  БЕЗ `keywords` и БЕЗ `sort` (широкая выдача AliExpress в её порядке),
  `page_no` 1-based + `page_size ≤ 50`.
  `supports_feed = True`, `supports_image_search = False` (см. §8).
* Парсинг ответа (`parse_products`) берёт только документированные поля:
  цена/старая цена/скидка, `total_tranpro3`-подобные счётчики заказов,
  `evaluate_rate` (сохраняется как отдельный `positive_rate`, НЕ конвертируется
  в звёзды), промо-поля `promo_code_info` (`promo_code`, `code_value` →
  `promo_benefit`, `code_availabletime_end` → `promo_valid_until`) — всё
  verbatim, без парсинга и домыслов.

### 10.3. Affiliate-ссылки — только настоящие

`aliexpress.affiliate.link.generate` требует обязательный `tracking_id`,
которого в проекте нет, поэтому генерация не вызывается вовсе.
`generate_affiliate_link()` возвращает **только** `promotion_link` —
подлинную affiliate-ссылку, которую API уже выдал в ответе
`product.query`/`productdetail.get`. Ни одной сфабрикованной ссылки:
`Product.best_url = affiliate_url or product_url`, аналитика различает
`affiliate_clicked` и `external_clicked`.

## 11. Gemini / AI (`services/ai_service.py`)

Модель выбирается по РОЛИ (fast/smart/vision/fallback, см. §4);
доступность обнаруживается только во время запроса — отсутствующая модель
никогда не останавливает бота. Классификация ошибок
(`_classify_gemini_error`) различает quota (account-wide, fallback НЕ пробуется)
и «модель недоступна» (один пробный вызов fallback-модели). Счётчики
`rank_requests/rank_fallback/image_requests` видны в админ-панели.

| Метод | Роль | Промпт / назначение | Фолбэк без AI |
|---|---|---|---|
| `parse_query` | fast | `QUERY_PARSING_PROMPT` → structured `SearchRequest` | детерминированный regex-парсер (валюты, цены, сортировка, ограничения качества) |
| `select_feed_candidates` | fast | `SELECT_PROMPT` + `STRICT_FILTER_RULES` — отбор ≤20 лучших карточек ленты из глубокого пула | детерминированный список (browse-cap) |
| `rank_candidates` | fast | `RANK_PROMPT` + те же правила — отбор/порядок кандидатов discovery-ленты | детерминированный порядок фида |
| `select_candidates` | fast | `SELECT_PROMPT` + те же правила — выбор/порядок кандидатов в AI-меню | порядок конвейера |
| `analyze_image` | vision | `IMAGE_ANALYSIS_PROMPT` — structured-чтение фото (§8) | честный «не удалось» |
| `describe_image_query` | vision | `IMAGE_QUERY_PROMPT` — legacy одна фраза | — |
| `analyze_product` | smart | `ANALYSIS_PROMPT` — разбор ОДНОГО товара по его реальным полям | карточка без AI-комментария |
| `answer_product_question` | smart | `QUESTION_PROMPT` — ответ ТОЛЬКО по переданным данным (≤8 товаров) | отказ |
| `plan_product_search` | fast | `SEARCH_PLAN_PROMPT` — короткий запрос «найти похожее» | заголовок товара как запрос |
| `merge_search_requests` | fast | `MERGE_PROMPT` — слияние уточнения с прошлым запросом | новый запрос как есть |

Все роли отбора (`select_feed_candidates`, `rank_candidates`, `select_candidates`)
судят кандидатов по ОДНИМ правилам (`STRICT_FILTER_RULES`: рейтинг ниже 4.5, не тот
товар/аксессуары, аномально дешёвый спам, дубли) и ограничены `TOP_CANDIDATES = 20`
в коде; id, которых нет в ответе, считаются отклонёнными и до карточек не доходят
(`feed_curation.apply_selection`), а выдуманный id отбрасывается.

Правило безопасности промптов: JSON-ответы валидируются по белым спискам
полей, лишние выбрасываются («вымуманное» поле не может отбраковать реальный
товар); в лог не пишутся ни ключи, ни сырые промпты.


## 12. Трекинг цен и уведомления

`services/price_tracker.py` подключается к JobQueue в `bot/__init__.py`:

* период — `TRACKING_INTERVAL_MINUTES` (0 отключает автоматику, UI честно
  говорит про ручной режим); цикл: активные строки `tracked_products` →
  fetch через того же провайдера, что и поиск → запись в `price_history` →
  уведомление при значимом изменении;
* значимое = сдвиг ≥ `DEFAULT_MIN_CHANGE_PERCENT` (1 %); меньшие пишутся
  в историю молча; первое наблюдение только задаёт baseline; идентичный
  ответ API не создаёт ни строки, ни уведомления;
* смена валюты (или неизвестная валюта) молча перебазирует baseline —
  процент в вакууме не враньё, поэтому таких уведомлений нет;
* каждое место проверяется в своём try/except — одна ошибка не ломает батч;
  невалидные данные пропускаются, подписка остаётся активной;
* уведомление несёт inline-кнопку «🛒 Купить» (affiliate-URL, честный
  fallback — обычный URL).

`services/notifications.py` — новостные/каталожные рассылки и напоминания
с уважением к пользовательским переключателям уведомлений на экране настроек.

## 13. Админ-панель и аналитика

`handlers/admin.py` — сетка из 13 секций поверх реальных данных, доступ
только для `ADMIN_IDS` (пусто = никто; отказ без подсказок, попытка в лог).
Секции читают SQLite и живые диагностики сервисов; панель НИКОГДА не
показывает: токен, API-ключи, подписанные URL, заголовки авторизации,
произвольный пользовательский текст (кроме top-запросов, разрешённых спекой).
Есть кнопка живого теста API (`adm:apitest`) и счётчики Gemini.

Аналитика (`services/analytics.py`, таблица `analytics_events`): события
`user_started, search_started/success/failed, products_shown, product_shown,
buy_clicked, product_opened, affiliate_clicked, external_clicked,
favorite_added/removed, tracking_added/stopped, compare_requested,
image_search_requested, currency_changed/mismatch, errors` — с user_id,
marketplace, product_id и `metadata_json`; дашборды админки группируют их
по периоду.

## 14. Пределы, фолбэки и принципы деградации

| Ситуация | Поведение |
|---|---|
| Нет `GEMINI_API_KEY` / quota / модель недоступна | regex-парсинг запросов, детерминированные ранжирование и фид; фото-поиск даёт короткий честный отказ |
| Нет ключей AliExpress | пустые результаты с честными текстами; весь UI жив |
| Фото > 5 МБ / скачалось с ошибкой | объясняющий текст + кнопка «попробовать другое фото» |
| Лента «✨ Находки» не находит нового (страницы исчерпаны) | честный экран «показывать те же товары повторно бот не будет» + `feed:reset` (легаси `pop:reset`), кэш-сессия не может обойти историю |
| Пропал кэш-сессия (`pnav` на протухший sid) | ничего не ломается: вежливый ответ/перерендер, состояние не выдумывается |
| Одна позиция трекинга упала | изолированный skip, батч продолжается |

Инварианты проекта: никаких выдуманных товаров/цен/скидок/ссылок/AI-итогов;
пользователю — короткий естественный текст, диагностика — в лог и админку;
бюджеты реальных API-вызовов ограничены конфигурацией с жёсткими потолками;
callback_data носит только `(session_id, index)`, а не товарные данные.

## 15. Запуск, тесты, smoke

```powershell
# Запуск бота
.venv\Scripts\python.exe main.py

# Юнит-тесты (полностью офлайн, 864 теста)
.venv\Scripts\python.exe -m unittest discover -s tests -v

# Офлайновый smoke discovery-потоков: реальные хендлеры на фейках
# tests.test_handlers_ui (текст-поиск, фид, промокоды, фото, трекинг)
.venv\Scripts\python.exe smoke_ux_flows.py          # SMOKE OK / SMOKE FAILED
```

`smoke_ux_flows.py` прогоняет реальные обработчики без сети и Telegram и
проверяет «карточный» контракт UX (запрещённые строки вроде «Найдено
товаров» / «Страница» / «Показать ещё»), включая заголовок ленты
«✨ Находки» на карточке.
`smoke_aliexpress.py` без ключей
возвращает SMOKE SKIPPED и не падает. Все скрипты — ручные проверки, они
не запускаются из тестового набора.

## 16. Краткая карта ключевых файлов

| Файл | Роль |
|---|---|
| `main.py` | точка входа, логирование, запуск polling |
| `bot/__init__.py` | DI-сборка приложения (общий httpx-клиент, job-трекинга) |
| `config/settings.py` | env → типизированные Settings с клампингом |
| `database/models.py` | DDL: 10 таблиц + 10 индексов |
| `services/search_service.py` | единый поисковый конвейер и `SearchOutcome` |
| `services/ai_service.py` | все Gemini-вызовы + regex-фолбэки + метрики |
| `services/marketplaces/aliexpress*.py` | провайдер + TOP-клиент (шлюз/подпись/ретраи) |
| `curation/pipeline.py`, `integration.py` | 5 стадий курации + гейт с журналом причин |
| `handlers/common.py` | DI-доступ, session-кэш (5 сессий), карточки, scopes |
| `handlers/discovery.py` | «✨ Находки» (G24-фид), категории, горячие скидки, фото |
| `handlers/search.py` | текстовый поиск и вся навигация по карточкам |
| `services/price_tracker.py` | job трекинга цен, порог 1 %, валюта-безопасность |
| `handlers/admin.py` | 13 секций диагностики, гейт ADMIN_IDS |
| `smoke_ux_flows.py`, `smoke_aliexpress.py` | интеграционные smoke-проверки |





