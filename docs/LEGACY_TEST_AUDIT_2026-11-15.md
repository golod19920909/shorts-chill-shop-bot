# Аудит модулей, исключённых из broad offline CI

**Дата:** 15 ноября 2026 года  
**Ветка:** `chore/repository-cleanup-2026-11-15`

## Назначение и границы

Этот документ фиксирует первичную классификацию модулей, которые явно исключены из broad offline pytest-команды в `.github/workflows/conversational-regression.yml`.

Это **не** утверждение, что все перечисленные тесты устарели. Проверка выполнена по импортам, названиям тестов и текущей архитектурной карте. Полный запуск каждого исключённого модуля и сверка каждого assertion с актуальным контрактом ещё не выполнены. Поэтому в рамках этой уборки тесты не удаляются, не получают blanket `xfail` и не возвращаются в обязательный CI без отдельной проверки.

## Классификация

| Модуль | Наблюдение по исходнику | Предварительный статус |
|---|---|---|
| `test_agent_service.py` | Импортирует `services.agent_service`, старый Agent/tool-loop API и `ConversationState`. | Подтверждённый кандидат на архивирование/переписывание под Option C; старый production-модуль не является текущей точкой входа. |
| `test_b3_routing_and_failures.py` | Импортирует `services.chat_router`, `ChatAction` и fixtures из старого `test_stage47_conversational.py`. | Legacy routing contract; не считать актуальным тестом authoritative engine без переписывания. |
| `test_b6_conversational_flow.py` | Использует старые chat-router и handler flow fixtures. | Legacy flow contract; требует переписывания на `ConversationSearchEngine`, прежде чем возвращать в обязательный CI. |
| `test_conversation_integration.py` | Интеграционные сценарии построены вокруг старого chat-router / ProductAssistant маршрута. | Переходный интеграционный тест; его бизнес-сценарии могут быть полезны, но wiring больше не является авторитетным. |
| `test_stage19b_offline_runner.py` | Тестирует отдельный исторический acceptance runner через динамическую загрузку файла. | Исторический диагностический harness; сначала проверить, поддерживается ли сам runner. |
| `test_stage19b_strict_addendum.py` | Проверяет внутренние функции того же исторического runner. | Исторический диагностический harness; не удалять без проверки зависимости runner. |
| `test_stage19c_offline.py` | Проверяет отдельный исторический offline acceptance script и его сетевые ограничения. | Исторический диагностический harness; проверить актуальность script entrypoint. |
| `test_stage21_classifier.py` | Проверяет исторический classifier/labeling script через динамическую загрузку. | Исторический диагностический harness; не относится автоматически к текущему conversational engine. |
| `test_stage47_conversational.py` | Импортирует `services.chat_router`, `services.conversation` и старый routing API. | Legacy routing suite; отдельные сценарии можно перенести в engine tests, но старый contract не должен управлять production behavior. |
| `test_stage49_followups.py` | Тестирует `services.chat_router` и `services.conversation.resolve_request`. | Legacy follow-up suite; требует переноса сценариев на текущий engine, а не механического включения. |
| `test_variant_b_conversation.py` | Небольшой набор тестов старого `chat_router`. | Legacy routing contract. |
| `test_discovery_acceptance.py` | Смешивает favorites/text/category-country acceptance tests с shown-history, menu wiring и Findings/feed behavior. | **Частично включён в focused CI:** `FavoritesScreenTests`, `ScreenTextTests`, `CategoryCountryPropagationTests`; защищённые history/menu/feed классы не затронуты и остаются исключёнными до отдельного аудита. |
| `test_gemini_telemetry.py` | Смешивает актуальные telemetry/instrumentation tests и исторический `AgentLoopTests`. | **Частично возвращён в focused CI:** пять текущих классов выполняются отдельно; старый AgentLoop-класс остаётся исключённым. |
| `test_ai_service.py` | Большой смешанный модуль текущего `AIService`; отдельные классы уже запускаются в focused suite, весь файл исключён из broad suite. | **Смешанный модуль, не stale целиком.** Разделить по операциям/контрактам; не включать или исключать весь файл одним решением. |
| `test_conversation.py` | Проверяет старый `services.conversation` state/merge helper. | Legacy helper contract; сверить оставшиеся call sites и перенести только действующие общие функции. |
| `test_stage3a_merge_feature_protection.py` | Тестирует merge constraints в старом `services.conversation.resolve_request`. | Legacy merge contract; не должен подменять тесты ledger/slot provenance текущего engine. |
| `test_stage3c_merge_constraint_protection.py` | Тестирует старый `deterministic_merge` / `resolve_request`. | Legacy merge contract; требует переноса актуальных инвариантов на текущий state ledger, если они ещё применимы. |
| `test_latency_and_failures.py` | Смешивает актуальные `services.convsearch.engine` и старые `chat_router` / `conversation` пути. | **Смешанный модуль.** Выделить engine instrumentation tests отдельно; не считать весь файл stale. |
| `test_result_limit_and_followup.py` | Использует текущие handlers, SearchService и text-matching для лимита карточек. | **Не подтверждён как устаревший.** Сценарии могут затрагивать защищённый «Умный поиск»; не менять assertions или production без отдельного разрешения. |
| `test_search_intent.py` | Тестирует текущий `query_intent`, но также старый `services.conversation` resolver. | **Смешанный модуль.** Отделить действующий intent contract от legacy merge path. |
| `test_session_isolation.py` | Использует текущие database, handlers, discovery и карточные callbacks; проверяет разделение состояния поиска и ленты. | **Не подтверждён как устаревший.** Включает взаимодействие защищённых потоков; не менять без отдельного разрешения. |

## Выводы

1. Нельзя называть все исключённые модули «устаревшими»: несколько тестируют действующие production-сервисы и пользовательские контракты.
2. Наиболее явный legacy-кластер — старый `chat_router` / `services.conversation` / Agent-tool-loop. Его тесты нельзя механически использовать как источник требований для Option C.
3. Смешанные тестовые файлы следует разделять по ответственности: текущие engine/telemetry/UI проверки отдельно от legacy merge/routing сценариев.
4. `test_discovery_acceptance.py`, `test_gemini_telemetry.py`, `test_result_limit_and_followup.py` и `test_session_isolation.py` требуют приоритетной адресной проверки, а не удаления.
5. Сценарии, затрагивающие «Умный поиск» и «✨ Находки», остаются защищёнными. Текущая узкая авторизация пользователя касается только исправления `empty_keyboard` в narrowing follow-up и не расширяется этим аудитом.

## Следующие безопасные действия

- Запускать исключённые модули по одному в изолированной CI-диагностике и сохранять полный traceback.
- Для каждого падения определять, устарел ли сам assertion, тестовая fixture или production behavior.
- Переносить полезные сценарии в тесты authoritative engine / текущих сервисов, не переписывая поведение под старые тесты.
- Возвращать в обязательный broad CI только модули, которые прошли отдельную проверку.
- До завершения аудита явно называть текущий broad CI **configured/filtered offline suite**, а не полным unfiltered suite.


### Follow-up после targeted CI

`test_gemini_telemetry.py` теперь частично запускается в focused CI. Первый запуск выявил устаревшее ожидание в timeout test: реализация делает один reserve retry, поэтому записывает две попытки. Ожидание исправлено на две попытки (primary + reserve); production-код не менялся. Проверка исправления ожидает повторного CI.


### Test hygiene follow-up

The broad-suite log showed that `BuyCallbackTests.test_products_without_affiliate_url_have_no_buy_button` had been declared async without any await under `unittest.TestCase`, so unittest did not await it and its assertions were not actually executed. It is now synchronous. This is test-only; CI run [#1945](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/43742653098) for commit `ff8b668d` passed, and the warning disappeared from the broad suite.


## Protected Smart Search baseline observation (historical state before full cleanup approval)

At the time of the original baseline audit, the owner had not yet approved
algorithm changes. The engine and tests were restored to baseline
`3822fc95`, and the audit recorded the then-current expectation mismatch in
`ConstraintAndFusionRegressionTests.test_partial_mandatory_feature_stays_hidden_if_gemini_times_out`.
This historical observation is not the current authorization state.

## Interim narrow authorization (16 November 2026; later superseded)

At an intermediate stage, the owner permitted only the test expectation change
from `unverified_required_features` to `rejected_by_type_gate`. At that point
the algorithm was intentionally left unchanged. A valid-empty continuation
expectation was also aligned with the existing implementation. The narrow
permission was subsequently superseded by explicit approval of the complete
repository-cleanup specification, recorded below.

The opt-in selector capture regression tests additionally verify redaction of
URLs, email addresses, phone numbers, Telegram IDs and credential-like values.
The later configured CI run confirmed these tests; see the superseding
authorization section.


## Superseding authorization — 2026-11-16

After the earlier narrow test-only authorization recorded above, the owner
explicitly approved execution of the full repository-cleanup technical
specification. That later approval supersedes the earlier statement that
algorithm changes were not authorized.

The approved P0 implementation now:
- uses the safe plus semantic-review-eligible pool for retrieval continuation,
  while keeping hard rejects outside the selector window;
- preserves bounded pages, selector windows, route order and prefetch
  cancellation;
- prevents a second Gemini pass after a valid empty response and limits valid
  partial continuation to unselected candidate IDs under the shared deadline;
- emits distinct continuation telemetry and structured counts/duration;
- distinguishes explicit incompatibility from missing feature evidence;
- keeps unknown mandatory-feature evidence quarantined and never treats a
  missing description phrase as proof of incompatibility.

The additional P0/P1 regression tests were confirmed by the configured CI run
on commit `6e481c2`: focused 598 passed, broad configured offline suite 2295
passed, with the documented deselections and one third-party deprecation
warning. The broad suite remains filtered; this is not an unfiltered run of
every historical test module.

The owner-side Telegram acceptance scenarios and the real 70-card AliExpress
gold-set with manual labels remain pending. No local .env, SQLite database,
backup, user PC or production data was accessed or modified.


## Targeted diagnostic execution — 2026-11-16

An isolated diagnostic workflow was added at
`.github/workflows/legacy-test-audit.yml`. It runs one module/class per matrix
job, installs the pinned Python 3.14 dependency graph, and is **manual-only**
(`workflow_dispatch`) so routine commits do not repeatedly install dependencies
for the entire diagnostic matrix.

Initial run [#43840032391](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/43840032391)
confirmed that the excluded set is mixed: several current contracts pass, while
some tests still assert retired helpers, old router wiring, or incomplete fakes.
No blanket skips, xfails, test deletions or production search behavior changes
were used to hide these results.

### Corrections made and now covered by focused CI

- `test_result_limit_and_followup.py`: two tests referenced the removed
  `_agent_follow_up_text` helper. They now exercise the current
  `_result_summary_text` function. All 11 tests pass; the module is in focused
  CI.
- `GroundedProductQuestionTests`: the service returns one optional
  recommendation ID, not a list of all claim IDs. The return annotation and
  docstring now match the implementation; tests distinguish a recommendation
  ID from claims-only answers. The class is in focused CI.
- `WiringGuardTests`: the old source guard expected parsing, merge and direct
  retrieval inside `handlers/search.py`. It now verifies the Option C boundary:
  the handler delegates to `ConversationSearchEngine.handle_message` and does
  not contain the retired parser/resolver pipeline. The class is in focused CI.
- `MenuWiringTests`: the fake category service now returns a real
  `SearchMode.CATEGORY` request with category code, normalized route and
  keywords. Tests also respect the existing 12-product category-screen
  minimum: a one-product result is not delivered or recorded as shown, while
  the category quality profile still accepts a valid $60 listing. This is
  test-fixture/expectation alignment only; production category behavior was
  not changed. The class is in focused CI.
- `test_session_isolation.py`: feed page expectations now match the live
  documented sampling contract (pages 1/40, then 2/41; next cursor 3), rather
  than the obsolete sequential 1/2/3/4 expectation. Five feed/history
  isolation scenarios are now selected individually in focused CI.

The latest verified run
[#43842771485](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/43842771485)
on commit `9cbf90f` passed:
- focused offline suite: **638 passed, 1 deselected, 593 subtests**;
- broad configured offline suite: **2,295 passed, 2 deselected, 1,259 subtests**;
- production compile, manual live-smoke syntax check, locked dependency
  consistency and `pip check`: PASS;
- one upstream Google GenAI SDK deprecation warning remains.

### Remaining diagnostic failures / boundaries

- Follow-up after run `43842771485`: `test_session_isolation.py` was
  corrected without changing production code. Its handler fixture now uses a
  deterministic Option C turn interpreter, and `card()` accepts only a real
  delivered photo-card caption (it no longer falls back to arbitrary bot text).
  The two previously unselected tests
  `test_a_new_search_never_inherits_the_previous_query` and
  `test_a_stale_card_browses_its_own_session_only` have now been added to the
  focused workflow. Their results are pending a CI run on the updated branch.
- Follow-up for `test_latency_and_failures.py`: the fake SearchService now
  accepts the current retrieval keyword contract, and the engine fixture uses a
  deterministic Option C turn interpreter. The current
  `EngineInstrumentationTests` class is now selected individually in focused
  CI. The two `RouterAIEscalationCapTests` still depend on removed
  `services.chat_router` and remain outside CI; do not restore the old router.
- The rest of `test_search_intent.py` and `test_ai_service.py` are mixed
  modules. Only the reviewed Option C wiring guard and grounded
  product-question class were promoted to focused CI; no entire file was
  declared stale or promoted wholesale.
- The historical Agent/tool-loop, old chat-router and stage acceptance
  harness modules remain excluded pending their own contract-by-contract
  migration. This diagnostic did not run every excluded module.


### Verification boundary after the follow-up fixture changes

The latest successful run recorded above (`43842771485`, commit `9cbf90f`)
predates the two additional session-isolation selections and the
`EngineInstrumentationTests` selection. The updated workflow is configured to
run them on the next branch push. Do not mark these new selections as passed
until that run completes successfully. The broad suite continues to exclude
the mixed modules as a whole; the targeted classes are separate focused tests.

No user-facing «Умный поиск» or «✨ Находки» production behavior was changed in
these follow-ups. No local `.env`, SQLite file, backups, user PC or production
data were accessed.
