# Журнал разработки

Инженерные заметки по интеграции: как устроен Open WebUI Filter, какие подводные
камни в Open WebUI v0.12.0 и в ядре обезличивания. Инструкция по установке —
в [README](../README.md).

Тесты и проверочная обвязка лежат отдельно (в скилле агента), в этом репозитории
их нет — здесь только сам плагин: Filter, HTTP-сервис и скрипты установки.

## Как устроена интеграция

```
Open WebUI                docx-anon-service (:8971)        LLM
     │                            │                           │
     │  inlet: вложение           │  POST /anonymize          │
     ├───────────────────────────►│  (ядро upstream + NER)     │
     │  обезличенный текст        │                           │
     │  + метки [ФИО_1]            │                           │
     │◄───────────────────────────┤                           │
     │                            │                           │
     │  запрос к модели (ПДн нет) ├──────────────────────────►│
     │                            │                           │
     │  outlet: ответ модели      │                           │
     │◄───────────────────────────┴───────────────────────────┤
     │  метки → исходные значения                           │
```


Цель: чтобы любой документ, попадающий в Open WebUI, обезличивался **до** того, как его увидит модель. Ниже — контракт, проверенный по исходникам тега v0.12.0 (https://github.com/open-webui/open-webui/tree/v0.12.0).

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




Одна команда, идемпотентная (можно запускать повторно — обновляет, а не ломает):

```bash
bash scripts/deploy_openwebui.sh
```

Что ей нужно:

| Требование | Как задать |
|---|---|
| SSH-доступ к хосту с Open WebUI (root) | `HA_HOST` (по умолчанию `192.168.1.4`), `HA_USER=root`, `HA_ROOT_PASSWORD` |
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

**Проверка на живом стенде** — скрипты проверки живут в скилле агента
(скрипт проверки (в скилле агента), 6 сквозных сценариев; скрипт проверки (в скилле агента) —
проверка через настоящий браузер), в репозиторий они не выкладываются.

Сценарии: DOCX с ПДн, PDF с ПДн, битый docx (fail-closed), `.xlsx` (блок),
картинка (обычный чат не ломается), чат без вложений.


### Классификация вложений в фильтре

Три класса — иначе либо ломается обычный чат, либо течёт ПДн:

| Класс | Примеры | Поведение |
|---|---|---|
| `document` | docx, docm, pdf, txt, md | обезличиваем |
| `media` | png, jpg, mp3, mp4 | пропускаем как есть (текстовых ПДн нет) |
| `unsupported` | xlsx, pptx, doc, odt | **не выпускаем** (fail-closed): обезличить нечем, а ПДн внутри возможны |

Определяем по расширению имени: `content_type` у Open WebUI часто `application/octet-stream` (MIME не всегда определяется). Нет ни расширения, ни MIME — считаем документом (безопаснее перестать).


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

