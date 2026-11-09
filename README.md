# AI Product Search Bot — Telegram-бот умного поиска товаров

Telegram-бот, который понимает запросы на естественном языке и ищет реальные
товары на AliExpress через официальное Affiliate API. Каждая цена, скидка
и ссылка приходит из ответа API — бот ничего не выдумывает: всё, что API
не вернуло, честно помечается как «нет данных».

Примеры запросов:

* `Find wireless headphones under $30 with good reviews`
* `Find a dash camera with night vision under $50`
* `Find this product cheaper`
* `Find 2 phone cases with a rating above 4.5`

## Главный принцип проекта

**AI не является источником данных о товарах.**

| Задача | Кто выполняет |
| --- | --- |
| Понимание каждого сообщения в контексте диалога | Gemini через валидированный структурированный ответ; при сбое — детерминированный fallback |
| Состояние диалога, ограничения и решение о следующем действии | Conversation Search Engine (`services/convsearch/`) |
| Получение реальных товаров, цен и партнёрских ссылок | Только AliExpress Affiliate API через существующий провайдер |
| Проверка типа товара, обязательных признаков, исключений и бюджета | Детерминированный Python-код |
| Семантический отбор | Gemini может выбирать только реальные ID из уже проверенного пула; ограничения остаются обязательными |
| Финальные карточки и отправка | Telegram handler; модель не создаёт товары и ссылки |

Gemini никогда не выдумывает товары, цены, рейтинги, наличие, стоимость доставки
или URL. Всё, что нельзя проверить, честно помечается как «нет данных».

## Архитектура

```
Telegram message
    │
    ▼
ConversationSearchEngine
    ├── Gemini interprets this turn using recent dialogue + active slot ledger
    ├── validates the proposed action; deterministic fallback on failure
    ├── updates product type, use case, features, exclusions, budget, preferences
    ├── handles clarification / comparison / alternatives without blind re-search
    ▼
RetrievalBackend
    └── existing SearchService → AliExpress API (bounded grounded query routes)
    ▼
Deterministic validation + filtering + ranking
    ├── product identity / required features / exclusions / hard budget
    └── Gemini may rank/select only IDs from the real validated candidate pool
    ▼
One SearchDecision
    ▼
Telegram handler renders real product cards and affiliate URLs
```

Ключевые архитектурные решения:

* **Option C — гибридный консультант.** Gemini понимает ход диалога и предлагает
  следующее действие в строгом формате. Conversation Search Engine проверяет
  этот ответ и управляет единственным журналом состояния пользователя.
* **Поиск остаётся детерминированным.** Существующий AliExpress provider и
  SearchService выполняют реальные запросы. Код проверяет тип товара,
  обязательные признаки, исключения и бюджет; Gemini не может отменить эти
  ограничения.
* **Отбор только среди реальных кандидатов.** Gemini получает ограниченный
  пул реальных карточек и может вернуть только их ID. При ошибке модели
  применяется безопасный fallback; неоднозначные совпадения не выдаются
  без подтверждения.
* **Один источник правды для диалога.** Тип товара, мягкий сценарий
  использования, обязательные признаки, исключения, бюджет и предпочтения
  хранятся в ledger. Ответ на уточнение продолжает текущий запрос, а новый
  товар сбрасывает ограничения предыдущего.
* **AliExpress only.** Другие маркетплейсы в текущей реализации не подключены.
* **Секреты только в `.env`.** Они не попадают в код и не должны публиковаться.

## Структура проекта

```
shorts-chill-shop-bot/
├── .env                     # локальные секреты (не коммитится)
├── .env.example             # шаблон конфигурации
├── .gitignore
├── requirements.txt         # рантайм-зависимости
├── requirements-dev.txt     # опционально: pytest
├── README.md
├── main.py                  # только запуск процесса
│
├── bot/
│   └── __init__.py          # фабрика Application, регистрация хендлеров, DI
│
├── config/
│   └── settings.py          # загрузка .env, MarketplaceCredentials, Settings
│
├── models/                  # Pydantic-модели (независимы от Telegram и API)
│   ├── product.py           # Product, Marketplace, Availability
│   └── search_request.py    # SearchRequest, SortPreference
│
├── database/
│   ├── models.py            # DDL-константы (users, history, favorites, ...)
│   └── database.py          # асинхронный слой доступа (aiosqlite)
│
├── handlers/                # Telegram-хендлеры
│   ├── common.py            # DI, кэш сессий, экранирование
│   ├── start.py             # /start, /help, главное меню
│   ├── search.py            # поиск, one-card навигация, кнопки карточек
│   ├── discovery.py         # «✨ Находки» (G24-фид), категории, поиск по фото (Gemini vision)
│   ├── hot_discounts.py     # экран «Горячие скидки» (4 раздела)
│   ├── region.py            # экран «Валюта» (пункт необязательный)
│   ├── favorites.py         # /favorites
│   └── settings.py          # /tracking, /stats, экран настроек
│
├── keyboards/
│   ├── main.py              # главное инлайн-меню (10 секций + канал)
│   ├── products.py          # клавиатуры товаров и списков
│   ├── hot_discounts.py     # клавиатура «Горячих скидок»
│   └── admin.py             # клавиатура /admin
│
├── curation/
│   ├── pipeline.py          # стадии: качество, boredom, дедупы, разнообразие
│   └── integration.py       # CurationGate: стык пайплайна с SQLite
│
├── services/
│   ├── ai_service.py        # Gemini (парсинг + vision) + локальный парсер
│   ├── search_service.py    # оркестрация поиска
│   ├── comparison_service.py# программная фильтрация/сортировка
│   ├── feed_curation.py     # хелперы фидов (near-dup, разнообразие, штраф)
│   ├── product_normalizer.py# нормализация значений: невидимые символы/BOM, пробелы, числа
│   ├── product_identity.py  # ключи дедупликации товаров
│   ├── relevance.py         # релевантность выдачи (пер-заголовочная)
│   ├── price_tracker.py     # планировщик проверки цен (JobQueue)
│   ├── analytics.py         # словарь событий и запись в analytics_events
│   └── marketplaces/
│       ├── base.py            # MarketplaceProvider (ABC)
│       ├── aliexpress.py      # AliExpress Affiliate API (поиск, детали, фид)
│       └── aliexpress_client.py # TOP-протокол: подпись, шлюз, разбор ответов
│
├── utils/
│   ├── logging.py           # логирование с маскированием секретов
│   └── formatting.py        # форматирование карточек
│
└── tests/                   # unittest, без сети и без реальных ключей
```

## Установка и запуск (Windows)

```powershell
cd C:\shorts-chill-shop-bot

# 1. Виртуальное окружение (уже создано в проекте)
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Зависимости
pip install -r requirements.txt
# опционально, для запуска тестов через pytest:
pip install -r requirements-dev.txt

# 3. Конфигурация
Copy-Item .env.example .env
# отредактируйте .env: как минимум BOT_TOKEN

# 4. Тесты
python -m unittest discover -s tests -t . -v

# 5. Запуск
python main.py
```

Требуется Python 3.14 (проверено на 3.14.6). Версия не менялась.

## Конфигурация (.env)

Все секреты хранятся только в `.env` (файл в `.gitignore`). Шаблон — `.env.example`.

| Переменная | Обязательна | По умолчанию | Назначение |
| --- | --- | --- | --- |
| `BOT_TOKEN` | да | — | токен от [@BotFather](https://t.me/BotFather) |
| `GEMINI_API_KEY` | нет | — | Google Gemini; без ключа работает локальный парсер, а фильтрация/сортировка считаются детерминированно (лента «✨ Находки» от Gemini не зависит вообще) |
| `GEMINI_PRIMARY_MODEL` | нет | `gemini-3.1-flash-lite` | основная модель для диалога, AI-подбора, сравнения, вопросов о товарах и распознавания фото |
| `GEMINI_RESERVE_MODEL` | нет | `gemini-3.8-flash` | резервная модель: один вызов после допустимой ошибки основной модели; не используется при account-wide quota/rate-limit |
| Старые `GEMINI_MODEL`, `GEMINI_SMART_MODEL`, `GEMINI_FAST_MODEL`, `GEMINI_VISION_MODEL`, `GEMINI_FALLBACK_MODEL` | — | — | больше не читаются; используйте две переменные выше |
| `FEED_OPEN_SIZE` | нет | `20` | «✨ Находки»: сколько товаров максимум загружается в одну feed session — объём RAM-pool, из которого карточки выдаются без повторных API-запросов (1..100) |
| `FEED_MAX_PAGES_PER_OPEN` | нет | `2` | «✨ Находки»: максимум страниц AliExpress (`page_no`, `page_size=50`), запрашиваемых за одно новое открытие ленты (1..10) |
| `ALIEXPRESS_APP_KEY` | нет | — | ключ AliExpress Open Platform |
| `ALIEXPRESS_APP_SECRET` | нет | — | секрет AliExpress Open Platform |
| `ADMIN_IDS` | нет | пусто | Telegram id администраторов через запятую; пусто = панель `/admin` недоступна никому |
| `ANALYTICS_DASHBOARD_PASSWORD` | нет | выключено | включает read-only веб-панель расходов Gemini; задайте длинный уникальный пароль |
| `ANALYTICS_DASHBOARD_USERNAME` | нет | `admin` | логин HTTP Basic для веб-панели |
| `ANALYTICS_DASHBOARD_HOST` | нет | `127.0.0.1` | адрес привязки; по умолчанию доступ только с этой машины |
| `ANALYTICS_DASHBOARD_PORT` | нет | `8765` | порт веб-панели |
| `TRACKING_INTERVAL_MINUTES` | нет | `1440` | период автопроверки цен (JobQueue) в минутах: по умолчанию 24 часа; `0` отключает автоматическую проверку |
| `DATABASE_PATH` | нет | `database/shop_bot.db` | путь к SQLite; относительный — от корня проекта |
| `LOG_LEVEL` | нет | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

Провайдер считается подключённым, только если заданы **и ключ, и секрет**.
Неизвестный `LOG_LEVEL` не является ошибкой — используется `INFO`.

Секреты никогда не пишутся в логи: `utils/logging.py` устанавливает фильтр,
маскирующий значения переменных окружения с секретными именами.

## Проверка состояния

Команда `/stats` в боте показывает, что подключено:

```
⚙️ Состояние бота

AI-парсинг запросов: не настроен — используется локальный парсер

Маркетплейсы:
• AliExpress — нет API-ключей
```

## Учёт расходов Gemini

При каждом вызове Gemini бот сохраняет usage metadata (входящие, выходящие,
thinking и cached tokens), модель, операцию, длительность и результат вызова.
История хранится в той же SQLite базе, указанной в `DATABASE_PATH`; тексты
промптов и сообщения пользователей в эту таблицу не записываются.

Стоимость — оценка по опубликованным тарифам Google Standard paid tier, а не
прямая выгрузка из биллинга AI Studio. Для нераспознанной модели или отсутствующих
usage metadata стоимость остаётся неизвестной, а не подменяется нулём.

Администратору доступны команды Telegram:
- `/stats_today` — расходы за сегодня (UTC);
- `/stats_week` — последние 7 дней;
- `/stats_month` — последние 30 дней;
- `/stats_models` — разбивка по моделям;
- `/stats_requests` — последние сгруппированные запросы.

Для read-only веб-панели задайте `ANALYTICS_DASHBOARD_PASSWORD` в локальном
`.env`. После запуска откройте `http://127.0.0.1:8765` и войдите по HTTP
Basic с указанными логином и паролем. По умолчанию сервер слушает только
loopback-интерфейс. На сервере оставляйте его за HTTPS reverse proxy; не
публикуйте Basic Auth напрямую через открытый HTTP. Панель не меняет настройки
бота и не вызывает Gemini.

## Тестирование

```powershell
# всё сразу (стандартная библиотека, без сети и без реальных ключей)
python -m unittest discover -s tests -t . -v

# по модулям
python -m unittest tests.test_settings -v
python -m unittest tests.test_product_model tests.test_search_request -v
python -m unittest tests.test_product_normalizer -v
python -m unittest tests.test_database -v
python -m unittest tests.test_marketplace_providers -v
python -m unittest tests.test_promotions -v   # guard'ы удалённых фич
python -m unittest tests.test_search_service tests.test_ai_service -v
python -m unittest tests.test_bot_startup -v

# офлайн-smoke всех потоков обнаружения (карточка сразу, без счётчиков и списков)
python smoke_ux_flows.py     # печатает SMOKE OK / SMOKE FAILED
```

Альтернативно через pytest (не требуется):

```powershell
pip install -r requirements-dev.txt
pytest -q
```

Что покрыто: загрузка конфигурации, модель `Product`, модель `SearchRequest` и
локальный парсер, нормализация значений маркетплейсов, инициализация БД и CRUD,
поведение провайдеров без ключей и с mock-транспортом (поиск, детали, фид),
деградация поиска при ошибке источника,
программная фильтрация/сортировка, сборка `Application` и полный офлайн-прогон
пайплайна «старт → поиск → shutdown».

Тесты **не требуют** реальных ключей маркетплейсов и не делают сетевых запросов.

## Подключение реальных API

Провайдер AliExpress использует официальное Affiliate API (TOP-протокол,
шлюз `https://api-sg.aliexpress.com/sync`). Детали протокола — подпись
`md5` / `hmac`, формат `timestamp`, разбор конверта `<method>_response` и
кодов ошибок — задокументированы в
`services/marketplaces/aliexpress_client.py`. Реализованы и проверены
вживую для этого ключа: `product.query`, `productdetail.get`.

**1. Получить официальные доступы** — AliExpress Affiliate Open Platform
(App Key + App Secret).

**2. Заполнить `.env`** (значения нужных пар ключ/секрет):

```dotenv
ALIEXPRESS_APP_KEY=ваш_ключ
ALIEXPRESS_APP_SECRET=ваш_секрет
```

**3. Проверить подключение:**

```powershell
python -m unittest discover -s tests -t . -v   # офлайн-тесты с mock-транспортом
python main.py                                 # бот сообщит о настроенных источниках
```

Честные ограничения также задокументированы в коде: `hotproduct.query`
отвечает этому ключу `InsufficientPermission`, поиск по картинке требует
недоступный `app_signature` — вместо подделки бот так и говорит
пользователю.

### Тестовые данные

Тесты используют mock-транспорт `httpx`, воспроизводящий задокументированные
ответы API. Для локальной разработки UI используйте свои классы-заглушки,
наследующие `MarketplaceProvider`, и передавайте их в
`SearchService(providers=[...])` — это ровно то, что делают тесты
(`tests/test_search_service.py`).

## Деплой на Linux-сервер (следующий этап)

```bash
sudo apt update && sudo apt install -y python3.14 python3.14-venv git
sudo useradd -m -s /bin/bash shopbot
sudo -iu shopbot
git clone <repo-url> ~/shorts-chill-shop-bot
cd ~/shorts-chill-shop-bot
python3.14 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env      # BOT_TOKEN и ключи
python main.py                         # проверка запуска
```

`systemd`-юнит (`/etc/systemd/system/shopbot.service`):

```ini
[Unit]
Description=AI Product Search Telegram Bot
After=network-online.target

[Service]
Type=simple
User=shopbot
WorkingDirectory=/home/shopbot/shorts-chill-shop-bot
ExecStart=/home/shopbot/shorts-chill-shop-bot/.venv/bin/python main.py
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now shopbot

# Daily SQLite backup (03:15 local server time)
sudo cp deploy/shopbot-backup.service /etc/systemd/system/
sudo cp deploy/shopbot-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now shopbot-backup.timer
systemctl list-timers shopbot-backup.timer

# One-time: allow shopbot to restart/check ONLY this systemd service.
# Create with: sudo visudo -f /etc/sudoers.d/shopbot-deploy
# File contents:
# shopbot ALL=(root) NOPASSWD: /usr/bin/systemctl restart shopbot, /usr/bin/systemctl is-active --quiet shopbot

# Guarded update from the deployed branch (default: main); run as shopbot
cd ~/shorts-chill-shop-bot
DEPLOY_REF=main bash scripts/deploy_update.sh

sudo journalctl -u shopbot -f
```

Обновление существующей установки:

* Скрипт требует чистую рабочую копию, текущую ветку `main` (или явно
  заданную `DEPLOY_REF`) и fast-forward обновление. Он создаёт проверенную
  SQLite-копию до изменения кода, обновляет только runtime-зависимости в
  рабочем `.venv`, создаёт временное изолированное окружение для pytest,
  компилирует приложение, запускает целевые регрессионные тесты и только
  затем перезапускает сервис. Тестовые зависимости не остаются в production `.venv`.
* Для запуска от пользователя `shopbot` настройте sudoers-правило выше:
  оно разрешает только `restart` и `is-active` для службы `shopbot`.
  Секреты и содержимое `.env` скрипт не читает и не меняет.
* При ошибке после начала обновления скрипт возвращает Git-код к предыдущему
  commit, восстанавливает runtime-зависимости и проверяет повторный запуск
  службы. SQLite автоматически не откатывается: бэкап сохраняется отдельно,
  чтобы не потерять новые пользовательские данные. Если потребуется откат
  несовместимой миграции, БД восстанавливается отдельно после остановки бота.
* Грязная рабочая копия, другая текущая ветка или не-fast-forward обновление
  приводят к безопасному отказу до изменения кода. Это серверная Linux/systemd
  процедура, не для локальной Windows-разработки.

Замечания:

* бот использует long polling, поэтому входящие порты открывать не нужно;
* SQLite-файл лежит в `database/`. Для согласованного бэкапа используйте
  `python -m scripts.database_maintenance backup`: утилита использует SQLite
  backup API, проверяет `PRAGMA integrity_check` и хранит последние 14 копий
  в `database/backups/` (или `DATABASE_BACKUP_DIR`). Для восстановления сначала
  остановите systemd-сервис, затем выполните
  `python -m scripts.database_maintenance restore <backup-file> --confirm-restore`.
  Перед заменой создаётся отдельная копия текущей базы; WAL/SHM sidecar-файлы
  сохраняются рядом с резервными файлами. После восстановления проверьте
  целостность базы через `/admin → База данных` и запустите сервис.
* TLS-терминация, nginx и webhook потребуются только при переходе на webhook;
* при переезде на несколько инстансов потребуется отдельная БД (например,
  PostgreSQL) — слой доступа изолирован в `database/database.py`.

## Что уже реализовано (текущий этап)

* Конфигурация из `.env` с безопасным поведением при отсутствии значений.
* Логирование с маскированием секретов, обработка ошибок без падения бота.
* Pydantic-модели `Product` (маркетплейс-независимая) и `SearchRequest`.
* Абстрактный `MarketplaceProvider` + реальный провайдер AliExpress
  (Affiliate API, TOP-протокол: `product.query`, `productdetail.get`).
* Асинхронный слой SQLite: `users`, `search_history`, `favorites`,
  `saved_products`, `tracked_products`, `price_history`.
* `AIService` (Gemini) с детерминированным локальным парсером как fallback.
* `SearchService`: параллельный опрос провайдеров, устойчивость к сбоям.
* `comparison_service`: программная фильтрация и сортировка (без AI в цифрах).
* `curation` + `services/feed_curation`: общий гейт курации для
  поиска, категорий и фото-фидов — валидность (URL/affiliate/заголовок) →
  профиль качества `$1..$20 / ≥300 заказов` → boredom → история →
  семьи → картинки → детерминированное ранжирование → разнообразие.
  Лента «✨ Находки» (G24) этим гейтом НЕ проходит: `show_feed` передаёт
  `pre_curated=True`, и на страницу `product.query` применяется только
  техническая очистка (нормализация → дедуп → картинка → affiliate →
  история показов). Статистика открытого фида — `/admin → ✨ Находки`
  (`last_feed_stats`: страница, сырые строки, дубли, скрыто как
  уже показанное). Список «запрещённых» слов-джанка (`BANNED_KEYWORDS`)
  живёт в `curation/pipeline.py` и применяется только к курируемым
  потокам, не к «✨ Находки».
* `relevance` + `comparison_service`: релевантность считается ПЕР-ЗАГОЛОВОК
  (скрипт заголовка выбирает, какие слова запроса вообще сравнимы), поэтому
  смешанный запрос «RU+EN» не отключает фильтр; сортировка — обычный Python.
* Telegram-интерфейс: командное меню Telegram (`/start`, `/help`, `/favorites`,
  `/tracking`, `/stats`, `/admin`), компактное инлайн-меню — 10 секций
  («Найти товар», «Найти по фото», «Находки», «Горячие скидки»,
  «Категории», «Избранное», «Отслеживание цен», «Валюта», «Настройки»,
  «Помощь») + URL-кнопка канала `https://t.me/Shorts_Chill`,
  **one-card навигация** — сразу карточка первого товара, без сводки,
  счётчика и списка ссылок; та же карточка редактируется на месте
  («◀️ Назад» / «➡️ Следующий»), без флуда сообщениями в чат.
* Кнопки карточки: «🛒 Купить» сначала вызывает callback бота, который
  записывает `affiliate_link_requested` и отправляет точную партнёрскую
  ссылку API отдельной URL-кнопкой (только если ссылка действительно есть).
  Telegram не сообщает, открыл ли пользователь последующую URL-кнопку:
  статистика отражает запрос ссылки, а не подтверждённое открытие/покупку.
  Также доступны «❤️ В избранное», «🔔 Следить за ценой» /
  «🔕 Не отслеживать» и «🔗 Поделиться» (нативный `t.me/share`). Кнопки «🔍 Сравнить» на карточке
  нет; разделы «Сравнить»/«Выгодные находки»/«Промо»/«Случайная находка»
  удалены — их старые кнопки отвечают tombstone и открывают меню.
* Навигация меняет карточку целиком: изображение, текст и клавиатура всегда
  принадлежат ОДНОМУ товару (`edit_message_media` с новой фотографией,
  подписью и клавиатурой; при смене типа сообщения фото↔текст — замена
  сообщения).
* Реальный поиск, карточки товаров, лента «✨ Находки» (G24:
  `aliexpress.affiliate.product.query` без `keywords` и без `sort`,
  документированная пагинация `page_no`/`page_size=50` — собственная
  широкая выдача AliExpress, техническая очистка вместо ранжирования,
  per-user курсор и история показов в `popular_state`/`popular_shown`,
  RAM-pool до `FEED_OPEN_SIZE` карточек, партнёрские ссылки — реальный
  `promotion_link` из ответа API) и 12 категорий — всё из живых ответов
  API, с дедупликацией и пер-заголовочным фильтром релевантности.
  Каждое открытие «✨ Находки» продолжает персональный курсор страницы,
  поэтому два подряд открытия не спрашивают одно и то же; уже показанное
  этому пользователю исключается по его истории.
* Поиск по фото честно двухшаговый: Gemini vision описывает объект на
  фотографии → обычный поиск AliExpress по этому описанию. Распознанный
  запрос идёт в поиск первым, и ровно один запасной вариант (короткая
  версия из двух слов) используется, только если по нему ничего не нашлось.
  Товары, цены и ссылки никогда не выдумываются; без ключа Gemini бот сразу
  и коротко сообщает, что распознавание недоступно.
* Реальное автоматическое отслеживание цен: планировщик JobQueue проверяет
  цены по `TRACKING_INTERVAL_MINUTES`, пишет историю и уведомляет об
  изменении ≥1% (без спама одинаковыми ответами).
* Многоходовой поиск: «Нужны беспроводные наушники до $30» → «А с хорошим
  микрофоном?» уточняет предыдущий запрос (бюджет, признаки, категория
  сохраняются; новый несвязанный запрос начинает поиск заново). Контекст
  живёт в памяти пользователя 30 минут и в базу не пишется.
* AI-меню на карточке товара («🤖 AI»): «Стоит ли покупать?», «Найти дешевле»,
  «Найти похожий», «Подобрать лучше», «Задать свой вопрос». Все кандидаты
  приходят из того же `SearchService` → `AliExpressProvider`; модель умеет
  только упорядочивать реальные `id`, любая выдуманная ссылка на товар
  отбрасывается, а цифры сверяются с полями `Product`.
* Запросы «удиви меня» / «что-нибудь прикольное для машины» идут в тот же
  конвейер с широким seed из слов пользователя; явный бюджет («до $20»)
  сохраняется и применяется к реальным кандидатам.
* 557 тестов, все проходят без сети и без реальных ключей (mock-транспорт
  httpx воспроизводит задокументированные ответы; интеграционные тесты
  планировщика — на реальной временной SQLite).

## Что намеренно НЕ реализовано

Честный список того, чего сейчас нет — в коде для этих случаев нет
подделок, бот прямо говорит пользователю правду:

| Возможность | Состояние |
| --- | --- |
| Живая приёмка Gemini | **не проверена**: квота Free Tier была исчерпана во время приёмки. Архитектура покрыта офлайн-тестами с подменёнными ответами модели; реальный запрос будет выполнен отдельно после восстановления квоты |
| Реальные вызовы API AliExpress | реализованы и проверены вживую для этого ключа: `product.query`, `productdetail.get`. Исключения честно задокументированы в коде: `hotproduct.query` отвечает `InsufficientPermission`, поиск по картинке требует недоступный `app_signature` — вместо подделки бот так и говорит пользователю |
| Партнёрские ссылки | `generate_affiliate_link()` возвращает настоящий `promotion_link` из ответа API; отдельная генерация через `aliexpress.affiliate.link.generate` не реализована сознательно: метод требует `tracking_id` и недоступный этому аккаунту уровень доступа |
| Поиск по изображению | у AliExpress обратный поиск по картинке недоступен (`supports_image_search = False`), поэтому бот делает честно двухшаговый поиск: Gemini vision описывает объект → обычный поиск AliExpress. Фальшивой «распознавалки» и выдуманных товаров нет |
| Единая валюта для сортировки и лимитов | сравнение идёт в валюте маркетплейса (или в предпочтении пользователя); конвертации нет |
| Промокоды | «🎟 Промокоды» — одна кнопка-ссылка: открывается ровно сконфигурированный партнёрский адрес (`config/promo.py::PROMO_REFERRAL_URL`), переданный Telegram дословно. Список кампаний и `featuredpromo.*` в UI не вызываются; старые кнопки `pcamp:<i>` получают tombstone-ответ и показывают текущий экран |
| События кликов «Купить» | «Купить» — URL-кнопка: Telegram не сообщает боту о нажатиях, поэтому `buy_clicked`/`affiliate_clicked`/`external_clicked`/`product_opened` больше не записываются (исторические строки в статистике сохраняются) |

## Дальнейшие шаги

1. Добавить серверный кэш результатов поиска и ограничение частоты запросов
   к API (rate limiting) сверх текущего интервала планировщика.
2. Развёртывание на Linux через `systemd` (см. раздел «Деплой»).
3. Добавить в избранное сортировку и повторный поиск «дешевле» по
   сохранённому товару.
4. Конвертация валют для честной сортировки и лимитов в разных валютах
   (сейчас цены сравниваются только внутри одной валюты).





### Обслуживание базы данных

`python -m scripts.database_maintenance backup` создаёт согласованную SQLite-копию через встроенный backup API, проверяет её целостность и применяет ротацию (14 последних файлов). Путь можно переопределить через `DATABASE_BACKUP_DIR`. В репозитории есть `deploy/shopbot-backup.service` и `.timer` для ежедневного запуска в 03:15; после установки проверьте `systemctl list-timers shopbot-backup.timer`. Команда `list` показывает копии. Восстановление выполняется только после остановки бота: `python -m scripts.database_maintenance restore <backup-file> --confirm-restore`; перед заменой создаётся pre-restore backup. Локальные копии исключены из Git.


### Партнёрская аналитика и заказы

В `/admin → Монетизация` отображаются запросы партнёрских ссылок и импортированные строки заказов по статусам. Администратор может вручную запустить ограниченную синхронизацию за последние 30 дней (до двух страниц на статус). Сохраняются только allowlisted поля заказа/товара и суммы `estimated_*_commission` как оценки; покупательские данные и `custom_parameters` не сохраняются. Пустой ответ API не означает доказанное отсутствие покупок за пределами проверенного окна. Telegram не сообщает об открытии URL-кнопки, поэтому `affiliate_link_requested` — это запрос ссылки, а не подтверждённый переход или заказ.


### Календарь распродаж

Текущая запись внутреннего календаря — «Чёрная пятница», начало 19.11.2026 (дата из кабинета Portals, сообщённая владельцем проекта); дата окончания не задана и не выдумывается. Администратор видит даты в `/admin → Акции`. Серверная команда `python manage_sales.py dates black_friday YYYY-MM-DD [YYYY-MM-DD]` обновляет календарные даты в `bot_config`; пользовательская страница акции показывает даты и ведёт на настроенную партнёрскую страницу. Карточки товаров кампании не показываются без подтверждения участия через API.


Подробная схема и оставшиеся release gates: `MONETIZATION_ARCHITECTURE.md`.


Границы доступных методов, seller API и текущие пробелы live-проверки: `docs/ALIEXPRESS_PORTALS_CAPABILITY_MATRIX.md`.


В разделе «Избранное» кнопка «Купить» появляется только у сохранённых товаров с действительной партнёрской ссылкой из исходного ответа API. Нажатие повторно читает сохранённый снимок товара для текущего пользователя, учитывается как `affiliate_link_requested` и выдаёт отдельную URL-кнопку. Это запрос ссылки, а не подтверждённый переход или покупка.


## Добровольный подбор аксессуаров

После нажатия «Купить» бот отдельно предлагает подобрать аксессуары или
дополнения к выбранному товару. Второй поиск запускается только после
нажатия «Да, подобрать» и отдельного текстового запроса пользователя.
Отказ не меняет текущий поиск и не запускает дополнительных запросов.
Подбор выполняется обычным AliExpress-поиском; товары не добавляются в
выдачу автоматически. Принятый запрос начинает новый контекст, чтобы
ограничения предыдущего товара не переносились на аксессуар.
