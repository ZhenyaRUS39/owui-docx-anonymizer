# Журнал разработки и заметки

Технические заметки по интеграции: как устроен Open WebUI Filter, найденные подводные камни, результаты тестов. Инструкция по установке — в [README](README.md).

## Что это

Расширение Chrome/Edge [argmaster-dev/docx-anonymizer](https://github.com/argmaster-dev/docx-anonymizer) (MIT, автор Алексей Федоров, Sitimlab) обезличивает `.docx` и PDF **перед** отправкой в ИИ: находит персональные данные и реквизиты, заменяет их плейсхолдерами (`[ФИО_1]`, `[ИНН_1]`), а ответ ИИ потом восстанавливает по карте замен.

Ключ для этого скилла: **`src/core/` расширения — чистый JavaScript без единого `chrome.*` и без сети**. Поэтому тот же движок работает в Node-сервисе, в n8n Code-node, в MCP-сервере и в Open WebUI Pipe — без браузера.

Проверено на 2026-11-01: `npm ci` → `npm run build` → 1948 unit/integration тестов зелёные, 85 Playwright e2e зелёные.

**Замер полноты на heldout-корпусе (12 документов с gold-разметкой, 82 gold-сущности):**

| Режим | Найдено | Утечки | Комментарий |
|---|---|---|---|
| только правила и словари | 54/82 (66 %) | **5** | утекли ФИО без меток: Нурланов, О'Коннор, Танака, Raghunathan, Wiśniewski |
| **+ модель NER** | 68/82 (83 %) | **0** | «пропуски» — варианты записи (инициалы против полных форм); сами фамилии замаскированы |

Вывод: **без модели NER обезличивание неполно** — фамилии третьих лиц без меток остаются в тексте. Модель обязательна для сквозного сценария.

**Сверка с upstream.** Автор проекта измеряет сам: `npx node scripts/eval-genres.mjs --dir=tests/fixtures/heldout [--model=<каталог модели>]`. Мой измеритель даёт те же числа:

| | upstream eval-genres | мой test_pipeline |
|---|---|---|
| без NER: утечки по жанрам | letter=2, memo=3, итого 5 | letter=2, memo=3, итого 5 |
| с NER: утечки | 0 | 0 |

Методика совпадает: утечки считаются только по категориям `pii`+`ids` (`ALWAYS`), потому что коммерческие (`COMPANY`/`CONTRACT`/`AMOUNT`) по политике 0.4 маскируются по желанию. Проверка утечек — поиск значения во всех XML-частях пакета тремя способами (сырой XML, с раскрытыми сущностями, без тегов), как в `tests/integration/support/pipeline.js`.

Прогон скилла на тех же 12 документах: `scripts/test_pipeline.py` — 24/24 кейсов (12 DOCX + 12 PDF из жанрового корпуса), `unresolved=0`, утечек нет.
Мутационная проверка теста: `scripts/mutation_check.py` и `scripts/mutation_check_b.py` — 6 из 6 поломок пойманы (тест не «всегда зелёный»).

**Полный прогон (2026-11-01): 61/61 кейс основного pipeline + 18/18 расширенных + 8/8 edge + 10/10 Open WebUI.**

| Набор | Кейсов | Что проверяет |
|---|---|---|
| `test_pipeline.py` | 61 | heldout + жанровый корпус (DOCX + PDF): обезличивание → утечки → восстановление → фильтр (inlet/outlet) |
| `test_extended.py` | 18 | PDF-специфика (поворот, две колонки, моноширинный, альбомная, 50 стр.), TXT/MD/DOCM, режимы `mask`/`delete`, идемпотентность, двойное восстановление |
| `test_edge_cases.py` | 8 | корректный отказ: PDF с паролем, пустой PDF, скан, не-docx, .xlsx, битый zip, нет файла, кириллица в имени |
| `test_openwebui_filter.py` | 10 | фильтр Open WebUI: файлы вычищены, модель получила текст, метки восстановлены в ответе |
| `mutation_check.py` + `_b.py` | 6 | тест обязан падать на сломанном коде |

Пути определяются автоматически (`scripts/paths.py`): checkout upstream и каталог модели NER ищутся в `/tmp/docx-anonymizer`, `/opt/docx-anonymizer` и др. Все тесты запускаются **без ручных флагов**:

```bash
bash scripts/run_suite.sh                    # все наборы
bash scripts/run_suite.sh pipeline extended  # выборочно
```

## When to Use

- Нужно обезличить договор, счёт, анкету,简历, ТЗ или PDF перед отправкой в LLM (Cloud, Open WebUI, n8n).
- Нужен восстановительный проход: вернуть настоящие значения в обезличенный документ или в ответ модели.
- Нужно проверить чужой документ на утечки ПДн (проверка «а что вообще уедет в сеть»).
- Нужно встроить обезличивание в Open WebUI как Pipe/Filter или Tool.

**Не использовать** для: сканов и PDF без текстового слоя (OCR нет), `.xlsx`/`.pptx` (ядро их не читает), и как «гарантию 100 % обезличивания» — ложные пропуски возможны всегда, поэтому нужен ручной просмотр находок.

## Prerequisites

- Node.js 22+ (`node -v`).
- Исходники расширения (ядро) — клон:

```bash
git clone https://github.com/argmaster-dev/docx-anonymizer.git /opt/docx-anonymizer
cd /opt/docx-anonymizer && npm ci
```

- Модель NER (опционально, но нужна для ФИО третьих лиц без меток): пакет `docx-anonymizer-ner-rubert-tiny2-ner-ruen-1.1.0.zip` из [GitHub Releases](https://github.com/argmaster-dev/docx-anonymizer/releases) — распаковать в каталог модели. Проверенный sha256: `5616d089ed34cb2e299bb1f8af0adb5863e29fa8e8784b37197694f30deea0b8`.

Скрипт находит ядро автоматически: `--repo <путь>`, переменная `DOCX_ANON_REPO`, либо `./docx-anonymizer`, `../docx-anonymizer`, `/opt/docx-anonymizer`.

## How to Run

```bash
# Обезличить документ (обязательна карта замен — без неё восстановление невозможно)
node scripts/anonymize.mjs --repo /opt/docx-anonymizer \
  --in договор.docx --out договор.anon.docx --map договор.map.json --commercial --json

# Восстановить по карте
node scripts/anonymize.mjs --repo /opt/docx-anonymizer \
  --restore договор.anon.docx --map-in договор.map.json --out договор.restored.docx
```

`scripts/anonymize.mjs` в этом скилле — готовый Node-обёртка над `src/core/`: разбирает `.docx`/`.docm`, PDF (pdf.js legacy из `node_modules`) и `.txt`/`.md`, прогоняет `analyze()` → `applyDecisions()`, пишет обезличенный файл и карту, умеет обратный проход.

## Quick Reference

| Флаг | Смысл |
|---|---|
| `--in <файл>` | исходный документ (`.docx`, `.docm`, `.pdf`, `.txt`, `.md`) |
| `--out <файл>` | куда записать обезличенный документ |
| `--map <файл.json>` | карта замен (обязательна для восстановления) |
| `--restore <файл>` + `--map-in <карта.json>` | обратный проход по карте |
| `--repo <путь>` | корень docx-anonymizer |
| `--commercial` | включить маскирование организаций, номеров договоров и сумм (в 0.4 выключено по умолчанию) |
| `--mode placeholder\|mask\|delete` | режим замены: плейсхолдер / маска `X` / удаление |
| `--ner <каталог>` | подключить модель NER (rubert-tiny2 int8) |
| `--no-ner` | только правила и словари, без модели (быстрее) |
| `--ner-threshold` / `--ner-threshold-org` | пороги модели (по умолчанию 0.7 / 0.85) |
| `--ner-stoplist "a,b"` | стоп-лист модельных организаций |
| `--language auto\|ru\|en\|both` | язык детекторов |
| `--placeholder-language document\|ru\|en` | язык плейсхолдеров |
| `--strip-embedded` | вырезать встроенные OLE-объекты |
| `--keep-metadata` | оставить метаданные docProps (по умолчанию они вычищаются) |
| `--json` | статистика в JSON (для агентов) |

## Настройки расширения (полный список)

Ядро читает те же поля, что и расширение (`src/core/settings.js`, `defaultSettings()`).

**Политика 0.4 — группы категорий.** Три группы, 21 категория:

- **ПДн (`pii`) — маскируются всегда, выключить нельзя:** `PASSPORT` (паспорт), `SNILS`, `CARD` (карта), `EMAIL`, `PHONE`, `BIRTHDATE`, `PERSON`, `ADDRESS`.
- **Идентификаторы (`ids`) — всегда:** `INN`, `KPP`, `OGRN`/`OGRNIP`, `ACCOUNT` (р/с, к/с), `IBAN`, `SWIFT`, `BIK`, `TAXID` (иностранные), `SSN`, `URL`.
- **Коммерческая информация (`commercial`) — по переключателю `commercial`, по умолчанию выключено:** `COMPANY` (названия организаций), `CONTRACT` (номер договора), `AMOUNT` (сумма).

Остальные поля `defaultSettings()`:

| Поле | По умолчанию | Смысл |
|---|---|---|
| `interceptEnabled` | `false` | перехват в браузере (в Node не нужно) |
| `siteMode` | `allowlist` | `all` / `allowlist`; в allowlist по умолчанию chatgpt.com, claude.ai, gemini.google.com, chat.deepseek.com, www.perplexity.ai |
| `maskedCompanies` | `[]` | список компаний, которые маскируются **всегда**, даже при выключенном `commercial` (до 200 записей, с псевдонимами) |
| `mode` | `placeholder` | `placeholder` / `mask` / `delete` |
| `language` | `auto` | язык детекторов |
| `placeholderLanguage` | `document` | язык плейсхолдеров (`[ФИО_1]` / `[PERSON_1]`) |
| `nerEnabled` | `true` | использовать модель NER |
| `nerThreshold` | `0.7` | порог для PER/LOC |
| `nerThresholdOrg` | `0.85` | порог для ORG — выше, иначе модель даёт ложные организации |
| `nerStoplist` | `[]` | стоп-лист модельных организаций (до 500) |
| `keepMetadata` | `false` | метаданные docProps вычищаются по умолчанию |
| `stripEmbedded` | `false` | встроенные OLE-объекты не обезличиваются — только предупреждение |
| `maxFileSizeMB` | `50` | лимит размера (1–100) |
| `exclusions` | `[]` | пары `{value, category}` — что не маскировать |
| `govEntities` | `true` | распознавать государственные организации |
| `dictNames` | `true` | словарный детектор ФИО третьих лиц |
| `customCategories` | `[]` | свои категории (до 100) |
| `profiles` | `llm`, `external` | два встроенных профиля: «Для LLM» и «Для внешней передачи» (второй включает все категории, выключает `govEntities`, включает `stripEmbedded`) |
| `batchOutput` / `outputSuffix` | `folder` / `.anon` | пакетная выгрузка в браузере |
| `pdfMode` | `markdown` | `markdown` / `text` / `off` — что уходит в чат из PDF |

Маскирование имён компаний по умолчанию выключено — если в задаче нужны «Ромашка» → `[КОМПАНИЯ_1]`, включать `commercial` или добавлять компанию в `maskedCompanies`.

## Procedure

**Обезличивание документа**

1. `terminal(command="node scripts/anonymize.mjs --repo <путь> --in <файл> --out <файл.anon> --map <файл.map.json> --commercial --json")`.
2. Проверить, что в `--json` ответе `byCategory` содержит ожидаемые категории, а `warnings` пуст (непустой `warnings` = не обезличено что-то, например `EMBEDDED_NOT_PROCESSED`).
3. Проверить утечки: искать в выходном файле исходные значения (`ИНН`, ФИО, e-mail, номера счетов). Скрипт `scripts/check_leaks.py` делает это по карте замен автоматически.
4. Передать `*.anon.*` в LLM. Карту `*.map.json` хранить отдельно и не отправлять в чат — в ней исходные значения.

**Восстановление**

5. `terminal(command="node scripts/anonymize.mjs --restore <файл.anon> --map-in <файл.map.json> --out <файл.restored>")`.
6. Убедиться, что `unresolved` в ответе равен 0: нераскрытые метки означают, что документ правился вручную и метки изменились.

**Проверка перед внедрением** (разово, на эталоне)

7. `terminal(command="cd <репозиторий> && npx vitest run")` — ожидается 1948 passed, 35 skipped, 2 todo.
8. `terminal(command="cd <репозиторий> && npm run build:e2e && npx playwright test")` — ожидается 85 passed, 7 skipped.
9. `terminal(command="cd <репозиторий> && npx playwright test nfr.spec.js")` — проверка «нулевой сети»: e2e снимает трафик всех целей браузера через CDP и требует ноль http(s) наружу.

**Прогон этого скилла на своих документах**

10. `terminal(command="bash scripts/run_suite.sh")` — все наборы сразу: pipeline (61 кейс), extended (18), edge (8), Open WebUI (10), мутации (6). Пути определяются автоматически. Ожидается `ВСЕ НАБОРЫ ПРОЙДЕНЫ`.
11. `terminal(command="python3 scripts/test_pipeline.py --with-genres --with-pdf --limit 61")` — основной путь на всём корпусе: обезличивание → утечки → восстановление + фильтр (inlet/outlet). Ожидается `61/61`.
12. `terminal(command="python3 scripts/test_extended.py")` — PDF-специфика, TXT/MD/DOCM, режимы `mask`/`delete`, идемпотентность, двойное восстановление. Ожидается `18/18`.
13. `terminal(command="python3 scripts/test_edge_cases.py")` — корректный отказ вместо падения: PDF с паролем, скан, .xlsx, битый zip. Ожидается `8/8`.
14. `terminal(command="python3 scripts/test_openwebui_filter.py")` — фильтр: файлы вычищены, модель получила текст, метки восстановлены. Ожидается `10/10`.
15. `terminal(command="python3 scripts/check_gold_coverage.py --fixtures <путь> --map-dir <каталог с картами>")` — полнота против gold-разметки, если она есть. Без NER полнота ~66 % и 5 утечек; с NER — 83 % и 0 утечек.
16. `terminal(command="python3 scripts/mutation_check.py && python3 scripts/mutation_check_b.py")` — тест обязан падать на сломанном коде. Если показывает 10/10 на мутациях — тест бесполезен, правь критерий.
17. `terminal(command="python3 scripts/check_leaks.py --map <карта> --anon <файл>")` — на **оригинале** обязана находить утечки (иначе проверка ничего не проверяет), на обезличенном — ноль.

**Сверка с upstream** (если есть сомнения в своих цифрах)

18. `terminal(command="cd <репозиторий> && npx node scripts/eval-genres.mjs --dir=tests/fixtures/heldout --model=<каталог модели>")` — авторский измеритель: утечки по жанрам, точность/полнота по категориям. Должен совпасть с твоим `test_pipeline.py`.

## Open WebUI (v0.12.0)

Цель: чтобы любой документ, попадающий в Open WebUI, обезличивался **до** того, как его увидит модель. Ниже — контракт, проверенный по исходникам тега v0.12.0 (снимок: `/config/owui_research/v0120`).

### Точки расширения

- **Filter — правильный выбор.** Класс `Filter` с хуками `inlet`/`request`/`outlet`. `inlet` вызывается в `backend/open_webui/utils/middleware.py:2691` (`process_filter_functions(..., filter_type='inlet')`) — **до** ухода запроса в модель; `outlet` — в `middleware.py:4199`, после ответа. Тело хука — это `form_data`.
- **Pipe** — `backend/open_webui/functions.py`, класс `Pipe` с методом `pipe(self, body)`. Pipe **заменяет модель целиком** (он сам делает вызов LLM), а не перехватывает запрос. Для обезличивания хуже, чем Filter.
- **Tool** — `backend/open_webui/routers/tools.py`, `POST /api/v1/tools/create`. Инструмент вызывает **модель** — значит обезличивание случится уже после того, как модель увидела текст. **Для ПДн не годится.**
- Тип функции определяется **по имени класса в исходнике** (`backend/open_webui/utils/plugin.py:296-306`): `Pipe` → pipe, `Filter` → filter, `Action` → action, `Event` → event. Отдельного «pipe function type id» нет — в `FunctionForm` поля `type` тоже нет.
- Фильтры применяются только к **активным** функциям; глобальные (`is_global`) — ко всем моделям (`backend/open_webui/utils/filter.py:59-65`).

### Что лежит в `body` в момент `inlet`

| Ключ | Состояние в `inlet` | Комментарий |
|---|---|---|
| `body["messages"]` | сообщения **без** ключа `files` | `files` вычищается в `process_messages_with_output` (`middleware.py:2371`) — раньше `inlet` |
| `body["files"]` | **список вложений** | клиент шлёт его на верхнем уровне тела (`Chat.svelte:4120`); pop — на `middleware.py:2790`, то есть **после** `inlet` |
| `body["metadata"]["files"]` | **тот же список вложений** | заполняется в `main.py:1329` (`metadata = {... 'files': form_data.get('files', None) ...}`) — **до** `inlet`; позже перезаписывается на `middleware.py:3011` |
| `body["model"]`, `body["params"]`, `body["filter_ids"]` | есть | `filter_ids` — список выбранных фильтров |

Содержимого файла в `body` нет — только метаданные (`id`, `name`, `content_type`, `meta`, `url`). Значит, обезличивать вложение фильтр обязан сам.

Важно: `body["files"]` и `body["metadata"]["files"]` — это **один и тот же список** (список объектов). Чистить надо оба ключа, иначе список останется в теле под другим именем.

### Как достать содержимое файла изнутри Filter

```python
from open_webui.models.files import Files
from open_webui.storage.provider import Storage

f = await Files.get_file_by_id(file_id)   # models/files.py:153 (async!)
path = Storage.get_file(f.path)           # storage/provider.py:47 — local: путь как есть
raw = open(path, "rb").read()             # сырые байты (для s3/gcs/azure нужен их SDK)
text = f.data.get("content", "")          # уже извлечённый текст (docx→txt) — проще
```

Для `.docx`/`.pdf` лучше брать **сырые байты** и прогонять через ядро: извлечённый Open WebUI текст уже потерял форматирование и часть структуры.

### Почему оригинал всё равно может утечь, если не почистить `files`

Три пути, все мимо `inlet`:

1. **RAG** — `chat_completion_files_handler` читает `body["metadata"]["files"]` (`middleware.py:2086`).
2. **Native function calling** — встроенный инструмент `view_file` (`backend/open_webui/tools/builtin.py:2757`) читает `Files.get_file_by_id(file_id).data["content"]` по `id` из `metadata["files"]`. Модель может вызвать его сама.
3. **file_context** — `add_file_context` (`middleware.py:1818`) вставляет `<file id=.../>` в сообщение.

Поэтому в `inlet` обязательно: `body.pop("files")`, `body["metadata"].pop("files")` и удаление `files` из сообщений. Тогда модель получит только обезличенный текст, который мы подмешали в `body["messages"][-1]["content"]`.

Альтернатива — **файловый режим**: загрузить обезличенный файл через `POST /api/v1/files/` и подставить его `id` в `body["files"]`; тогда и RAG, и `view_file` возьмут обезличенную версию. Минус — нужен реальный file id, иначе RAG-список будет битый.

### Имя параметра хука — `__user__`

`get_filter_params` (`backend/open_webui/utils/filter.py:135-144`) собирает аргументы из `extra_params` **по совпадению имён** в сигнатуре хука. В `extra_params` есть `__user__`, `__metadata__`, `__request__`, `__model__`, `__event_emitter__`. Поэтому:

```python
def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:   # верно
def inlet(self, body: dict, user: Optional[dict] = None) -> dict:      # user будет всегда None
```

Шаблон Filter в самом UI Open WebUI содержит именно `user` — это баг шаблона, а не документации.

### Карта замен в Open WebUI

Карта (mapping) содержит исходные значения. Её нельзя отдавать модели и нельзя класть в `metadata` — только в своё хранилище, по ключу `(chat_id, message_id)` из `body["metadata"]`. В `outlet` по этому ключу метки в ответе модели заменяются настоящими значениями.

### Создание Filter через API

```
POST /api/v1/functions/create
Authorization: Bearer sk-<ключ>
Content-Type: application/json

{
  "id": "docx_anonymizer",
  "name": "DOCX Anonymizer",
  "content": "<python код фильтра>",
  "meta": {"description": "Обезличивает документы перед отправкой в модель"}
}
```

- `id`: только буквы/цифры/подчёркивание, `.isidentifier()` (`backend/open_webui/routers/functions.py:214`), имя приводится к нижнему регистру.
- В `FunctionForm` **нет** полей `type`, `access_control`, `is_active` — тип выводится из кода, а функция создаётся **неактивной** (`models/functions.py:169`).
- Включить: `POST /api/v1/functions/id/docx_anonymizer/toggle`
- Глобально (для всех моделей): `POST /api/v1/functions/id/docx_anonymizer/toggle/global`
- Привязка к конкретной модели: `model.info.meta.filterIds = ["docx_anonymizer"]`

API-ключ: `POST /api/v1/auths/api_key` (singular, с подчёркиванием) — возвращает `sk-<hex>`, использовать как `Authorization: Bearer sk-...`.

Полный рабочий код фильтра — `references/openwebui-filter.py`.

### Живая инсталляция (проверено 2026-11-01 на HA 192.168.1.4)

### Как установить

Одна команда, идемпотентная (можно запускать повторно — обновляет, а не ломает):

```bash
bash scripts/deploy_openwebui.sh
```

Что ей нужно:

| Требование | Как задать |
|---|---|
| SSH-доступ к хосту с Open WebUI (root) | `HA_HOST` (по умолчанию `192.168.1.4`), `HA_USER=root`, `HA_ROOT_PASSWORD` — если пароля нет в `/config/.hermes/secrets/ha_ssh.json` |
| docker + интернет на хосте | — |
| контейнер Open WebUI | определяется автоматически (`docker ps \| grep open-webui`), можно задать `OWUI_CONTAINER` |
| адрес Open WebUI внутри хоста | `OWUI_URL` (по умолчанию `http://127.0.0.1:8080`) |

Опционально: `BASE=/opt/docx-anon-service`, `PORT=8971`, `FILTER_ID=docx_anonymizer`,
`SKIP_LIVE_CHECK=1` (не гонять живую проверку), `NER_ASSET=…` (другая сборка модели NER).

Скрипт сам генерирует admin API-ключ (в исходниках нет ни одного постоянного ключа),
сам клонирует ядро upstream, качает модель NER, поднимает сервис, регистрирует
Filter, включает его и в конце прогоняет живую проверку из 6 сценариев.

Ожидаемый финал:

```
=== ГОТОВО
Фильтр: docx_anonymizer (active + global)
=== 6/6 живая проверка фильтра
[OK  ] L1 DOCX с ПДн
[OK  ] L2 PDF с ПДн
[OK  ] L3 битый docx (fail-closed)
[OK  ] L4 .xlsx (формат не обезличивается)
[OK  ] L5 картинка (обычный чат не ломается)
[OK  ] L6 чат без вложений
ИТОГ: 6/6
```

**Ручная установка** (если скрипт не подходит) — те же шаги по отдельности:

1. `git clone --depth 1 https://github.com/argmaster-dev/docx-anonymizer.git /opt/docx-anon-service/repo`
   и `npm ci` внутри (там `onnxruntime-web` и `pdfjs-dist` — без них не работает ни NER, ни PDF).
2. Модель NER: zip из релизов upstream в `/opt/docx-anon-service/ner` (файл `model.onnx`).
3. `references/anon_service.mjs` → `/opt/docx-anon-service/anon/`,
   `scripts/anonymize.mjs` → `/opt/docx-anon-service/anon/scripts/`.
4. Контейнер `docx-anon-service`: `node:22-alpine`, `--network host`,
   `--restart unless-stopped`, монтирования `repo:ro`, `ner:ro`, `anon:ro`, `state`.
   Переменные: `DOCX_ANON_REPO`, `DOCX_ANON_NER`, `DOCX_ANON_WRAPPER`,
   `DOCX_ANON_WORKDIR`, `DOCX_ANON_PORT`.
5. Filter: `references/openwebui-filter.py` как функция с id `docx_anonymizer`,
   `is_active=true`, `is_global=true`. Значения `Valves` — дефолтные
   (`SERVICE_URL=http://127.0.0.1:8971`): сервис доступен по localhost, потому что
   оба контейнера в host-network.

**Проверка, что работает** (вручную, без скрипта):

```bash
curl -s http://127.0.0.1:8971/health
# {"ok":true,"ner":true,"repo":"/srv/repo",...}
```

Open WebUI v0.12.0 в аддоне HA Supervisor, контейнер `app_4696b1a1_open-webui`, **network_mode=host**.
Три части:

| Часть | Где | Что это |
|---|---|---|
| ядро + модель NER | `/opt/docx-anon-service/{repo,ner}` на хосте HA | клон upstream + распакованный пакет модели |
| HTTP-сервис | контейнер `docx-anon-service` (node:22-alpine, `--network host`, `--restart unless-stopped`) | `references/anon_service.mjs`: `/health`, `/anonymize`, `/restore` |
| Filter | функция `docx_anonymizer` в БД Open WebUI | `references/openwebui-filter.py`, `is_active=true`, `is_global=true` |

**Развёртывание одной командой:** `bash scripts/deploy_openwebui.sh` (идемпотентно: ядро/модель не перекачивает, функцию обновляет, фильтр включает только если выключен, в конце гоняет живую проверку).

**Почему сервис, а не node внутри open-webui:** в контейнере open-webui node **нет вообще** (проверено). SPEC §5 прямо рекомендует «call your own local HTTP service … needs no Open WebUI credentials at all». Сервис слушает `127.0.0.1:8971` — из контейнера open-webui он доступен, потому что оба в host-network.

**Хуки Filter могут быть `async def`** — `utils/filter.py:159-162` (`run_filter_handler`) сам проверяет `inspect.iscoroutinefunction` и ждёт coroutine. А методы `Files.get_file_by_id` — async (`models/files.py:153`), поэтому хуки должны быть async.

**Проверка на живом стенде** (`scripts/test_live_deployed.py`, 6 сценариев):

```
L1 DOCX с ПДн   → до outlet: ПДн=нет меток=6; после: ПДн вернулись, меток=0
L2 PDF с ПДн    → то же
L3 битый docx   → оригинал в сообщениях: нет; предупреждение: да
L4 .xlsx        → оригинал в сообщениях: нет; предупреждение: да
L5 картинка     → ответ получен (обычный чат не сломан)
L6 без вложений → ответ получен, меток=0
```

Ответ после outlet сохраняется в БД (`originalContent` — метки, `content` — восстановленное). Это и есть то, что показывает UI: `chat:outlet` событие патчит `history.messages[msg.id]` (`Chat.svelte:1595`).

**API-ключ админа** можно создать напрямую в БД — ключ хранится в открытом виде (`models/users.py:433`: `ApiKey.key == api_key`). Нужна только запись `auth.enable_api_keys = true` в persistent-конфиге (таблица `config` в `webui.db`), иначе backend отвечает 403 «Use of API key is not enabled in the environment» (`utils/auth.py:523`).

### Классификация вложений в фильтре

Три класса — иначе либо ломается обычный чат, либо течёт ПДн:

| Класс | Примеры | Поведение |
|---|---|---|
| `document` | docx, docm, pdf, txt, md | обезличиваем |
| `media` | png, jpg, mp3, mp4 | пропускаем как есть (текстовых ПДн нет) |
| `unsupported` | xlsx, pptx, doc, odt | **не выпускаем** (fail-closed): обезличить нечем, а ПДн внутри возможны |

Определяем по расширению имени: `content_type` у Open WebUI часто `application/octet-stream` (MIME не всегда определяется). Нет ни расширения, ни MIME — считаем документом (безопаснее перестать).

### Доказательства на живом стенде (2026-11-01)

Проверка через настоящий браузер (Chromium → логин → вложение → вопрос → ответ):

```
B1  ответ ассистента: ПДн=Белоусов, Гусейнова...  меток=0   ✓ восстановлено
B2  POST /api/chat/completions: ПДн в теле=нет                ✓
B2b блок <attached_files> в запросе: нет                       ✓ id файла не передан
B3  аудит-лог фильтра: только метки                            ✓
```

Аудит-лог `/tmp/docx-anon/model_input.log` внутри контейнера open-webui показывает,
что **реально ушло в модель** — это независимое подтверждение, что ПДн не проходят
транзитом. Пишется самим фильтром при каждом inlet.

Проверка записи файла в БД: `files.data['content']` после фильтра содержит
только метки (5 шт.), исходных значений — ноль. Значит по id файла оригинал
недоступен ни RAG, ни `view_file`.

## Pitfalls

- **`Valves` обязан быть `pydantic.BaseModel`.** Backend вызывает `Valves.schema()` (`routers/functions.py:510`) и пересоздаёт `self.valves` перед каждым вызовом (`utils/plugin.py:64-70`). Обычный класс без `.schema()` → `AttributeError`, который проглатывается, и фильтр **молча не работает**. Это самая коварная ошибка: симптом — «ничего не происходит», а в логах только `AttributeError`.
- **Одной чистки `body["files"]` мало — оригинал остаётся достижимым по id файла.** `add_file_context` добавляет блок `<attached_files>` с id файла **до** inlet (`middleware.py:2369`), и по этому id модель может вызвать встроенный `view_file` (`tools/builtin.py:2757`), который читает оригинал из `files.data['content']`. Нужно: (1) убрать блок `<attached_files>` из текста сообщения; (2) перезаписать `files.data['content']` обезличенным текстом (`Files.update_file_data_by_id`).
- **`output` восстанавливать обязательно, но UI показывает не только его.** У сообщения ассистента есть поле `output` — список структурированных блоков (`reasoning`, `output_text`), и UI рисует именно его. Если восстановить только `content`, в БД будет чисто, а пользователь в UI увидит метки. Проверять надо оба: и БД, и DOM.
- **«Рукописный» PDF без встроенного юникод-шрифта извлекается как `????`** и ядро считает его сканом (`PDF_NO_TEXT`): стандартные шрифты PDF (WinAnsi) не умеют кириллицу. Рабочий PDF-фикстура — только с встроенным шрифтом (upstream генерирует их через Chromium). Ручная сборка PDF из `BT/Tj` для кириллицы не годится.
- **Первый запрос после обновления фильтра может использовать закешированный код.** Backend кэширует скомпилированную функцию; после деплоя первый запрос иногда уходит со старой версией. Поэтому живая проверка должна повторяться (retry), а не считаться провалом по первому прогону.
- **В `outlet` брать последнее сообщение с ролью `assistant`, а не `messages[-1]`.** В теле outlet сообщение пользователя может идти после ответа — тогда `msgs[-1]` указывает не на то сообщение и восстановление не срабатывает.
- **`toggle` и `toggle/global` — ПЕРЕКЛЮЧАТЕЛИ, а не «включить».** Повторный вызов выключает активный фильтр (`is_active=0`), и `get_active_filter_ids` его больше не отдаёт → фильтр исчезает из конвейера без ошибок. В деплое: сначала читаем `is_active`/`is_global`, и дёргаем toggle только если нужное состояние выключено.
- **Исключение из `inlet` НЕ блокирует запрос — оно его ПРОПУСКАЕТ.** В Open WebUI v0.12.0 `main.py:1700-1706`: при наличии `session_id` и `chat_id` исключение проглатывается (`except Exception: if not (session_id and chat_id): raise`), запрос всё равно уходит к модели — уже **без обезличивания**. Поэтому fail-closed реализуется НЕ через `raise`, а так: убрать оригинал из тела запроса + подставить плашку «документ не отправлен, потому что не удалось обезличить». Тогда ПДн физически не могут уйти.
- **В `inlet` надо чистить `files` и `metadata["files"]`, а в `outlet` — нет.** `outlet` получает другое тело (`middleware.py:4159-4180`): `chat_id`, `session_id`, `id`, `messages` — но **без** `metadata`. Ключ карты строим по `chat_id`/`session_id`, а не по `message_id` (у inlet и outlet он разный).
- **`outlet` обновляет БД и эмитит `chat:outlet`, а HTTP-ответ не патчит.** При `stream=True` и `session_id` HTTP-ответ — всегда `null` (штатное поведение: ответ идёт через socket.io). Проверять восстановление надо по БД чата, а не по телу HTTP-ответа.
- **Socket.io-путь: путь `/ws/socket.io`, все события в одном канале `events`.** Подписка на `chat:message`/`chat:outlet` не сработает — сервер эмитит `events` с вложенным `data` (`socket/main.py:1335-1345`). Комната — `user:<user_id>`, плюс `events:chat` с `type='join'` для комнаты `chat:<id>`.
- **`pydantic v2`: `Valves.model_fields["X"].default = ...` НЕ действует.** Присваивание в `model_fields` не меняет дефолт при создании модели. В тестах — подкласс с нужными дефолтами, а не правка `model_fields`.
- **Без модели NER обезличивание неполно.** На heldout-корпусе без NER утекли 5 фамилий третьих лиц (Нурланов, О'Коннор, Танака, Raghunathan, Wiśniewski) — фамилии без меток находятся только моделью. Всегда подключай `--ner` (в сервисе: `DOCX_ANON_NER`).
- **Регулярка по сырому XML docx ищет `<w:t>`, а не `{namespace}t`.** Пространство имён в сыром XML не подставлено — regex по `{...}t` находит НОЛЬ и тихо даёт «утечек нет». Используй `scripts/docx_text.py` (префиксный regex).
- **Определяй формат файла по сигнатуре, а не по расширению.** Переименованный `.anon` — это zip docx; если прочитать его как текст, утечки будут незаметны. `check_leaks.py` и `check_gold_coverage.py` уже так делают.
- **Ветка восстановления в `anonymize.mjs` должна определять формат по сигнатуре.** По расширению файл `.anon` уходит в текстовую ветку — на выходе двоичный мусор вместо docx.
- **Ядро склоняет ФИО при восстановлении** («Сафину Гульнару Маратовну» → «Сафина Гульнара Маратовна»), а карта хранит один канон. Проверять восстановление точным сравнением строк бессмысленно — сверяй по корню токенов (общий префикс) либо по факту «сущность вернулась в любой форме».
- **Проверка «утечек нет» без контрольного прогона на оригинале — бесполезна.** Прогоняй `check_leaks.py` на исходном файле: он обязан найти утечки. Если нашёл 0 на оригинале — парсер ничего не читает.
- **Утечки считаются только по категориям `pii`+`ids`** (`ALWAYS`), как в upstream: коммерческие (`COMPANY`/`CONTRACT`/`AMOUNT`) по политике 0.4 маскируются по желанию. Если считать все категории, получишь ложные падения на компаниях без ОПФ.
- **Названия компаний по умолчанию не маскируются.** В 0.4 политика поменялась: `commercial` выключен во всех профилях, включая миграцию 2 → 3. Если ожидаешь `[КОМПАНИЯ_1]` — включай `--commercial` или добавляй компанию в `maskedCompanies`.
- **Бренд разработчика (`sitimlab`) защищён от маскирования** — `NEVER_MASK_ORGS` в `src/core/detect/names/lexicon.js:66`. Формально это «своя компания», но при оценке независимости держи в голове.
- **Карта замен = исходные ПДн.** Хранить как секрет, не отправлять в LLM, не класть в чат.
- **PDF → не PDF.** Ядро отдаёт `.anon.md`/`.anon.txt` (текст с плейсхолдерами), а не PDF. Восстановление PDF не поддерживается — только `.docx`/`.docm`/`.md`/`.txt`.
- **Сканы и PDF без текстового слоя отклоняются** (`PDF_NO_TEXT`) — OCR нет. Пакет `pdfjs` требует текстовый слой.
- **Встроенные OLE-объекты не обезличиваются** — либо предупреждение `EMBEDDED_NOT_PROCESSED`, либо вырезать (`--strip-embedded`).
- **ФИО третьих лиц без метки находятся только моделью.** Без `--ner` в тексте «Ответственный: Сидоров» имя останется. Причина: без NER словарь ли не знает фамилию.
- **Модель может дать ложную организацию** («Кузнецова» как ORG). Порог ORG по умолчанию 0.85 — опускать его нужно осознанно, иначе маскируются обычные слова.
- **Node-обёртка требует `npm ci` в каталоге ядра** — там живут `onnxruntime-web` и `pdfjs-dist`, без них не работает ни NER, ни PDF.
- **`extractPdf` сам возвращает layout.** Не вызывай `buildLayout` второй раз — упадёт `pages.map is not a function`.
- **В Open WebUI `body` не содержит содержимого файла.** Обезличивать нужно самому: скачать, обработать, подменить.
- **Имя параметра хука — `__user__`, не `user`.** Аргументы собираются из `extra_params` по совпадению имён в сигнатуре (`utils/filter.py:135-144`). Шаблон Filter в UI Open WebUI содержит `user` — это баг шаблона: значение всегда будет `None`.
- **Оригинал файла утечёт тремя путями мимо `inlet`:** RAG (`metadata["files"]` → `middleware.py:2086`), встроенный инструмент `view_file` при native FC (`tools/builtin.py:2757`) и `file_context` (`<file id=.../>`). Чистить `files` обязательно.
- **`body["files"]` и `body["metadata"]["files"]` — один и тот же список** (первый заполняется клиентом, второй копируется в `main.py:1329` до `inlet`). Чистить оба ключа.
- **Функция создаётся неактивной** (`models/functions.py:169`) — после `POST /api/v1/functions/create` нужен `POST /api/v1/functions/id/<id>/toggle`.
- **Ключ Open WebUI — `/api/v1/auths/api_key`** (singular), формат `sk-<hex>`, заголовок `Authorization: Bearer sk-...`.
- **Ответ модели может содержать метки в другом написании** (`[ФИО_1]`, `ФИО_1`, `[ ФИО_1 ]`) — `deanonymize()` это понимает, но в строгом режиме (`strict`) заменяет только метки ровно как в карте.
- **Расширение не обновляется само** (unpacked-расширение) — новая версия ставится распаковкой поверх и кнопкой «Обновить» в `chrome://extensions`. ID закреплён полем `key` в `manifest.json`, настройки и модель переживают обновление.

## Verification

- Обезличивание доказано, когда: в выходном файле нет ни одного исходного значения из карты (проверяется `scripts/check_leaks.py`), а `byCategory` показывает ожидаемые категории.
- Восстановление доказано, когда: `unresolved == 0` и исходные значения вернулись в нужных падежах.
- Отсутствие сети в расширении доказано тестом `tests/e2e/nfr.spec.js` (CDP-запись трафика всех целей браузера) и ESLint-правилом `no-restricted-globals` для `fetch`/`XMLHttpRequest`/`WebSocket`/`EventSource` (единственное исключение — `src/offscreen/package-fetch.js`, читающий zip модели из пакета самого расширения).
- В Node-обёртке сети нет по построению: ядро не делает сетевых вызовов, а pdf.js в Node читает CMaps и шрифты с диска.

## Reference Files

- `scripts/anonymize.mjs` — Node-обёртка над ядром: обезличивание и восстановление для `.docx`/`.pdf`/`.txt`.
- `references/anon_service.mjs` — HTTP-сервис обезличивания для Open WebUI: `/health`, `/anonymize`, `/restore`. Чистый Node, без python (текст docx извлекается через `fflate`).
- `scripts/docx_text.py` — извлечение видимого текста из docx и поиск утечек по всем XML-частям (метод upstream).
- `scripts/check_leaks.py` — проверка утечек по карте замен; формат файла определяется по сигнатуре.
- `scripts/check_gold_coverage.py` — полнота обезличивания против gold-разметки (что найдено, что пропущено, что утекло).
- `scripts/test_pipeline.py` — главный тест: CLI + Open WebUI-фильтр (через поднятый локально HTTP-сервис), 8 утверждений на кейс.
- `scripts/mutation_check.py`, `scripts/mutation_check_b.py` — мутационные проверки: тест обязан падать на сломанном коде (3 + 4 мутации).
- `scripts/run_suite.sh` — прогон всех наборов: pipeline, extended, edge, Open WebUI, мутации.
- `scripts/test_extended.py` — 18 кейсов: PDF-специфика, TXT/MD/DOCM, режимы `mask`/`delete`, идемпотентность, двойное восстановление.
- `scripts/test_edge_cases.py` — 8 кейсов корректного отказа вместо падения (PDF с паролем, скан, .xlsx, битый zip).
- `scripts/test_openwebui_filter.py` — 10 запросов через Open WebUI-фильтр (модель Open WebUI замокана в `scripts/owui_mock/`).
- `scripts/test_live_deployed.py` — 6 сквозных сценариев на живом стенде (запускается внутри контейнера open-webui).
- `scripts/deploy_openwebui.sh` — идемпотентное развёртывание сервиса + фильтра на хост HA, в конце гоняет живую проверку.
- `scripts/browser_check.mjs` — браузерная проверка на настоящем стенде: Chromium → логин → вложение → вопрос → проверка DOM ответа, перехваченного запроса и блока `<attached_files>`.

  Запуск (логин/пароль — только через окружение, в файле их нет):

  ```bash
  OWUI_URL=http://192.168.1.4:8080 \
  OWUI_EMAIL=<логин> OWUI_PASSWORD=<пароль> \
  timeout 275 node scripts/browser_check.mjs /tmp/ui_test.docx
  ```

  Таймаут 275 с — не меньше: ответ модели на этом стенде занимает 20–60 с,
  а сам скрипт ждёт до 150 с. При меньшем таймауте процесс убивается до
  печати результата (выглядит как «завис», но ответ в БД уже записан).
- `references/openwebui-filter.py` — рабочий Open WebUI Filter (Python) для обезличивания вложений в `inlet`.
- `references/settings.md` — полный список настроек ядра с типами, миграциями и профилями.
