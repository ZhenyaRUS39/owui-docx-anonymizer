"""
Open WebUI Filter (v0.12.0): обезличивание вложений ДО того, как их увидит модель.

Контракт проверен по исходникам open-webui v0.12.0
(https://github.com/open-webui/open-webui/tree/v0.12.0):

  * Тип функции определяется по имени класса: Pipe | Filter | Action | Event
    (backend/open_webui/utils/plugin.py:296-306).
  * Хуки Filter: inlet / request / outlet.
    inlet  — backend/open_webui/utils/middleware.py:2691 (process_filter_functions(..., 'inlet'))
    outlet — backend/open_webui/utils/middleware.py:4199
  * ИМЯ ПАРАМЕТРА ПОЛЬЗОВАТЕЛЯ — `__user__`, не `user` (в штатном boilerplate это ошибка).
  * Хуки могут быть async: utils/filter.py:159-162 (run_filter_handler) сам проверяет
    inspect.iscoroutinefunction и ждёт coroutine. Методы Files.* — async, поэтому
    inlet/outout здесь async.
  * В inlet у сообщения пользователя ключ "files" УЖЕ УДАЛЁН (middleware.py:2371);
    список вложений лежит в body["files"] (верхний уровень) — он ещё доступен,
    его выпиливают только на middleware.py:2790, ПОСЛЕ inlet.
    body["metadata"]["files"] заполняется на middleware.py:3011 — тоже после inlet.
  * file_handler: если у фильтра есть self.file_handler = True, backend вычищает
    список файлов после inlet (utils/filter.py:243-248) — оригинал не доедет ни до
    RAG, ни до LLM.
  * Содержимое файла: Files.get_file_by_id(id).data["content"] (уже извлечённый текст)
    либо сырые байты через Storage.get_file(file.path).
  * Восстановление ответа: outlet получает другой body, без metadata — карту ведём
    по (chat_id, session_id) в WORKDIR.

КАК РАБОТАЕТ ОБЕЗЛИЧИВАНИЕ:
  Фильтр НЕ запускает node сам (в контейнере open-webui node нет вообще).
  Он ходит по HTTP в локальный сервис обезличивания (SPEC §5 — «call your own
  local HTTP service … needs no Open WebUI credentials at all»):
      POST http://127.0.0.1:8971/anonymize  multipart: file=<байты>
      POST http://127.0.0.1:8971/restore    json: {"text": ..., "map": ...}

УСТАНОВКА:
  POST /api/v1/functions/create   (id="docx_anonymizer", admin)
  POST /api/v1/functions/id/docx_anonymizer/toggle
  POST /api/v1/functions/id/docx_anonymizer/toggle/global
"""

import json
import os
import re
import time

import requests

# Valves ОБЯЗАН быть pydantic-моделью: backend вызывает Valves.schema()
# (routers/functions.py:510) и пересоздаёт self.valves перед каждым вызовом
# (utils/plugin.py:64-70). Обычный класс без .schema() ломает всю функцию:
# AttributeError проглатывается, и inlet не отрабатывает вовсе.
from pydantic import BaseModel, Field

# Блок <attached_files> добавляет backend ДО inlet (middleware.py:2369 →
# add_file_context). В нём id файла, по которому модель может вызвать
# view_file и прочитать оригинал. Убираем блок целиком.
_ATTACHED_FILES_RE = re.compile(
    r"<attached_files>.*?</attached_files>\s*", re.DOTALL | re.IGNORECASE
)


def _strip_attached_files(text: str) -> str:
    if not isinstance(text, str):
        return text
    return _ATTACHED_FILES_RE.sub("", text)


def _strip_attached_files_in_list(items):
    """content в формате list: [{'type':'text','text':...}, ...]."""
    out = []
    for it in items or []:
        if isinstance(it, dict) and isinstance(it.get("text"), str):
            it = dict(it)
            it["text"] = _strip_attached_files(it["text"])
        out.append(it)
    return out


class Filter:
    class Valves(BaseModel):
        # Адрес локального сервиса обезличивания (контейнер docx-anon-service,
        # host-network — 127.0.0.1:8971 доступен и из контейнера open-webui).
        SERVICE_URL: str = "http://127.0.0.1:8971"
        # Таймаут запроса в секундах (PDF на 50 страниц ~ 10-20 с).
        TIMEOUT: int = 300
        # Максимальный размер файла, МБ. Больше — не выпускаем (fail-closed).
        MAX_FILE_MB: int = 50
        # Что делать с документом, который не удалось обезличить:
        #   True  — НЕ выпускать его к модели (fail-closed): оригинал удаляется
        #            из тела запроса, пользователю показывается плашка.
        #            ВНИМАНИЕ: бросать исключение НЕЛЬЗЯ — при наличии
        #            session_id и chat_id Open WebUI проглатывает его и всё
        #            равно отправляет запрос к модели (main.py:1700-1706),
        #            то есть уже с оригиналом.
        #   False — выпустить как есть (ОПАСНО: ПДн уедут к модели).
        FAIL_CLOSED: bool = True
        # Максимальный размер ответа модели, который пропускаем через восстановление.
        MAX_RESTORE_CHARS: int = 200000
        # Хранить карту замен на диске (WORKDIR). Там лежат исходные ПДн.
        WORKDIR: str = "/tmp/docx-anon"
        # Сколько часов живёт карта замен. Карта нужна, пока идёт чат:
        # в inlet создаётся, в outlet читается. Дальше она — просто исходные
        # ПДн на диске, поэтому старые карты удаляются.
        MAP_TTL_HOURS: int = 24
        # Как часто чистить (в inlet, не чаще раза в N секунд).
        CLEANUP_INTERVAL_SEC: int = 300
        # Токен сервиса обезличивания. Обязателен, если сервис слушает
        # не только localhost (DOCX_ANON_HOST != 127.0.0.1).
        SERVICE_TOKEN: str = ""
        # Группа «Коммерческая информация» (названия компаний, товарные знаки
        # и часть реквизитов юрлиц).
        #   «Включено» — маскировать коммерческие данные (то же, что
        #     --commercial в ядре; по умолчанию сервис работает в этом режиме);
        #   «Выключено» — не маскировать: в документе останутся названия
        #     компаний. Персональные данные (pii/ids) маскируются ВСЕГДА —
        #     настройка на них не влияет.
        #   «Как в сервисе» — взять настройку сервиса (DOCX_ANON_COMMERCIAL).
        COMMERCIAL: str = Field(
            default="Как в сервисе",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": [
                        {"value": "Включено", "label": "Коммерческая: включено"},
                        {"value": "Выключено", "label": "Коммерческая: выключено"},
                        {"value": "Как в сервисе", "label": "Коммерческая: как в сервисе"},
                    ],
                }
            },
        )

        # ── Режим замены ПДн ──────────────────────────────────────────────
        #   «Метки»   — [ФИО_1], [ПАСПОРТ_1]: значения можно восстановить;
        #   «Звёздочки»— XXXXXX XXXX: значение не восстановить, но видно, что
        #                было что-то заменено;
        #   «Удалить» — [удалено]: значение удалено полностью.
        #   «Как в сервисе» — DOCX_ANON_MODE.
        MODE: str = Field(
            default="Как в сервисе",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": [
                        {"value": "Метки", "label": "Режим: метки ([ФИО_1])"},
                        {"value": "Звёздочки", "label": "Режим: звёздочки (XXXXXX)"},
                        {"value": "Удалить", "label": "Режим: удалить ([удалено])"},
                        {"value": "Как в сервисе", "label": "Режим: как в сервисе"},
                    ],
                }
            },
        )

        # ── Нейросеть (NER) ───────────────────────────────────────────────
        # Модель распознаёт ФИО и организации, которых нет в словарях и
        # правилах. Выключение ускоряет обработку, но часть имён останется
        # незамаскированной.
        NER_ENABLED: str = Field(
            default="Как в сервисе",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": [
                        {"value": "Включено", "label": "Нейросеть: включено"},
                        {"value": "Выключено", "label": "Нейросеть: выключено"},
                        {"value": "Как в сервисе", "label": "Нейросеть: как в сервисе"},
                    ],
                }
            },
        )

        # Порог уверенности нейросети (0..1). Ниже — больше имён находится,
        # но выше риск ложных срабатываний. 0.7 — значение ядра по умолчанию.
        NER_THRESHOLD: str = Field(
            default="Как в сервисе",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": [
                        {"value": "Как в сервисе", "label": "Порог нейросети: как в сервисе"},
                        {"value": "0.5", "label": "Порог нейросети: 0.5 (строже)"},
                        {"value": "0.7", "label": "Порог нейросети: 0.7 (по умолчанию)"},
                        {"value": "0.85", "label": "Порог нейросети: 0.85 (мягче)"},
                        {"value": "0.95", "label": "Порог нейросети: 0.95 (мягче всего)"},
                    ],
                }
            },
        )

        # ── Детекторы ─────────────────────────────────────────────────────
        # Госорганы: «Не трогать» — ФНС, МВД, Сбербанк и прочие из списка
        # ядра остаются как есть; «Маскировать» — маскируются наравне с
        # остальными организациями.
        GOV_ENTITIES: str = Field(
            default="Как в сервисе",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": [
                        {"value": "Не трогать", "label": "Госорганы: не трогать"},
                        {"value": "Маскировать", "label": "Госорганы: маскировать"},
                        {"value": "Как в сервисе", "label": "Госорганы: как в сервисе"},
                    ],
                }
            },
        )

        # Словарь имён: уточняет ФИО по большому словарю (уменьшает ложные
        # срабатывания на названиях компаний и наоборот).
        DICT_NAMES: str = Field(
            default="Как в сервисе",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": [
                        {"value": "Включено", "label": "Словарь имён: включено"},
                        {"value": "Выключено", "label": "Словарь имён: выключено"},
                        {"value": "Как в сервисе", "label": "Словарь имён: как в сервисе"},
                    ],
                }
            },
        )

        # ── Метаданные и встроенные объекты ───────────────────────────────
        # Метаданные DOCX (автор, компания, время правки) могут содержать ПДн.
        # По умолчанию ядро их вычищает.
        KEEP_METADATA: str = Field(
            default="Как в сервисе",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": [
                        {"value": "Вычищать", "label": "Метаданные: вычищать (безопасно)"},
                        {"value": "Оставлять", "label": "Метаданные: оставлять"},
                        {"value": "Как в сервисе", "label": "Метаданные: как в сервисе"},
                    ],
                }
            },
        )

        # Встроенные объекты (OLE-вложения, диаграммы) ядро не обезличивает —
        # при включении оно вырезает их из файла.
        STRIP_EMBEDDED: str = Field(
            default="Как в сервисе",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": [
                        {"value": "Вырезать", "label": "Встроенные объекты: вырезать"},
                        {"value": "Оставлять", "label": "Встроенные объекты: оставлять"},
                        {"value": "Как в сервисе", "label": "Встроенные объекты: как в сервисе"},
                    ],
                }
            },
        )

        # ── Языки ──────────────────────────────────────────────────────────
        # Язык меток: [ФИО_1] (по-русски) или [PERSON_1] (по-английски).
        # «Как в документе» — язык берётся из самого документа.
        PLACEHOLDER_LANGUAGE: str = Field(
            default="Как в сервисе",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": [
                        {"value": "Как в документе", "label": "Язык меток: как в документе"},
                        {"value": "ru", "label": "Язык меток: русский"},
                        {"value": "en", "label": "Язык меток: английский"},
                        {"value": "Как в сервисе", "label": "Язык меток: как в сервисе"},
                    ],
                }
            },
        )

        # Язык распознавания: на каком языке искать ФИО, адреса и компании.
        LANGUAGE: str = Field(
            default="Как в сервисе",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": [
                        {"value": "Как в документе", "label": "Язык поиска: как в документе"},
                        {"value": "ru", "label": "Язык поиска: русский"},
                        {"value": "en", "label": "Язык поиска: английский"},
                        {"value": "both", "label": "Язык поиска: оба"},
                        {"value": "Как в сервисе", "label": "Язык поиска: как в сервисе"},
                    ],
                }
            },
        )

    def __init__(self):
        self.valves = self.Valves()
        self.file_handler = True

    # ------------------------------------------------------------------
    # служебное
    # ------------------------------------------------------------------
    def _service(self, path: str) -> str:
        return self.valves.SERVICE_URL.rstrip("/") + path

    def _map_path(self, key: str) -> str:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (key or "unknown"))
        return os.path.join(self.valves.WORKDIR, f"{safe}.json")

    def _load_map(self, key: str):
        p = self._map_path(key)
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _save_map(self, key: str, data) -> None:
        try:
            os.makedirs(self.valves.WORKDIR, exist_ok=True)
            tmp = self._map_path(key) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self._map_path(key))
            os.chmod(self._map_path(key), 0o600)
        except Exception:
            pass

    def _cleanup_maps(self) -> None:
        """Удаляет старые карты замен.

        Карта — это исходные ПДн на диске (0600, только root). После чата
        она больше не нужна: в outlet карта читается сразу после ответа
        модели. Без очистки карты копятся бессрочно.
        """
        now = time.time()
        last = getattr(self, "_last_cleanup", 0)
        if now - last < self.valves.CLEANUP_INTERVAL_SEC:
            return
        self._last_cleanup = now
        ttl = max(0, int(self.valves.MAP_TTL_HOURS)) * 3600
        try:
            names = os.listdir(self.valves.WORKDIR)
        except Exception:
            return
        for name in names:
            if not name.endswith(".json"):
                continue
            p = os.path.join(self.valves.WORKDIR, name)
            try:
                if now - os.path.getmtime(p) > ttl:
                    os.remove(p)
            except Exception:
                pass

    def _session_key(self, body: dict) -> str:
        # outlet получает другое тело, поэтому ключ карты строим на устойчивых полях.
        meta = body.get("metadata") or {}
        for k in ("chat_id", "session_id", "id"):
            if body.get(k):
                return str(body[k])
            if isinstance(meta, dict) and meta.get(k):
                return str(meta[k])
        return "global"

    # Классификация вложений — три класса:
    #   MEDIA      — картинки/аудио/видео: пропускаем как есть (текстовых ПДн нет)
    #   DOCUMENT   — форматы, которые ядро обезличивает: обезличиваем
    #   EXTRACTED  — форматы, которые читает Open WebUI, но не ядро (xlsx/pptx/
    #                odt/doc): обезличиваем извлечённый текст
    #   UNSUPPORTED— форматы, которые не читает никто: БЛОКИРУЕМ, потому что
    #                обезличить нечем, а ПДн внутри вполне могут быть.
    DOC_EXTS = {".docx", ".docm", ".pdf", ".txt", ".md", ".text"}
    # Форматы, которые Open WebUI умеет разбирать сам (xlsx/pptx/odt/doc), но
    # которые ядро обезличивания не читает. Их текст Open WebUI извлекает в
    # files.data['content'] — обезличиваем именно его (как обычный текст).
    EXTRACTED_EXTS = {".xlsx", ".xls", ".xlsm", ".pptx", ".pptm", ".odt", ".ods", ".odp", ".doc"}
    MEDIA_EXTS = {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".tiff", ".tif",
        ".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac",
        ".mp4", ".webm", ".mov", ".avi", ".mkv",
    }

    @staticmethod
    def _file_items(body: dict):
        """Список вложений: сначала верхний уровень, потом metadata."""
        items = body.get("files") or []
        if items:
            return items
        meta = body.get("metadata") or {}
        return (meta.get("files") or []) if isinstance(meta, dict) else []

    @classmethod
    def _classify(cls, filename: str, meta: dict) -> str:
        """'media' | 'document' | 'unsupported'.

        Решаем по расширению имени: content_type у Open WebUI часто бывает
        application/octet-stream (MIME не всегда определяется).
        """
        name = (filename or "").lower()
        ext = os.path.splitext(name)[1] if "." in name else ""
        if ext in cls.DOC_EXTS:
            return "document"
        if ext in cls.EXTRACTED_EXTS:
            return "extracted"
        if ext in cls.MEDIA_EXTS:
            return "media"
        ctype = ((meta or {}).get("content_type") or "").lower()
        if not ext and ctype:
            if ctype.startswith(("image/", "audio/", "video/")):
                return "media"
            if ctype in {
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/msword",
                "application/pdf",
                "text/plain",
                "text/markdown",
            }:
                return "document"
        if not ext and not ctype:
            # Ни расширения, ни MIME — считаем документом: безопаснее перестать.
            return "document"
        return "unsupported"

    async def _scrub_file_record(self, file_id: str, anon_text: str):
        """Затираем ПДн в записи файла.

        Одной чистки body["files"] мало: оригинал остаётся в БД
        (files.data['content'] — уже извлечённый текст с ПДн), а его id
        живёт в блоке <attached_files>, который backend добавляет ДО inlet
        (middleware.py:2369 → add_file_context). По этому id модель может
        вызвать встроенный инструмент view_file (tools/builtin.py:2757) и
        прочитать оригинал. Поэтому перезаписываем содержимое записи
        обезличенным текстом — тогда ПДн не достать ни одним путём.
        """
        try:
            from open_webui.models.files import Files
            await Files.update_file_data_by_id(
                file_id,
                {"content": anon_text, "status": "completed", "error": None},
            )
        except Exception:
            # Не смогли затереть — не выпускаем id файла в теле запроса,
            # иначе view_file достанет оригинал. Лучше потерять вложение,
            # чем отдать ПДн.
            raise RuntimeError(f"не удалось обезличить запись файла {file_id}")

    async def _file_record(self, file_id: str):
        """Запись файла из БД: (имя файла, data). Байты не читаем."""
        from open_webui.models.files import Files

        file = await Files.get_file_by_id(file_id)
        if not file:
            return "", {}
        return (getattr(file, "filename", "") or "",
                (getattr(file, "data", None) or {}))

    async def _read_bytes(self, file_id: str):
        """Сырые байты файла через storage-провайдер."""
        from open_webui.models.files import Files
        from open_webui.storage.provider import Storage

        file = await Files.get_file_by_id(file_id)
        if not file:
            return None, None
        raw = b""
        try:
            if getattr(file, "path", None):
                path = Storage.get_file(file.path)
                with open(path, "rb") as f:
                    raw = f.read()
        except Exception:
            raw = b""
        if not raw:
            # Запасной путь: уже извлечённый текст (docx/pdf/txt → txt).
            content = (getattr(file, "data", None) or {}).get("content", "")
            if content:
                raw = content.encode("utf-8")
        return raw, getattr(file, "filename", "") or ""

    async def _read_extracted_text(self, file_id: str):
        """Текст, который Open WebUI уже извлёк из файла (xlsx/pptx/odt/doc).

        Для этих форматов байты файла ядру бесполезны (оно их не читает),
        а Open WebUI разбирает их сам и кладёт текст в files.data['content'].
        Именно этот текст уходит в модель — значит обезличивать надо его.
        """
        from open_webui.models.files import Files

        file = await Files.get_file_by_id(file_id)
        if not file:
            return None
        data = getattr(file, "data", None) or {}
        content = data.get("content")
        if not isinstance(content, str) or not content.strip():
            return None
        # Если обработка не завершилась (ошибка разбора) — текста нет.
        if data.get("status") not in (None, "completed"):
            return None
        return content

    def _auth(self) -> dict:
        # Заголовки для сервиса обезличивания. Токен обязателен, если сервис
        # слушает не только localhost (DOCX_ANON_HOST).
        token = (self.valves.SERVICE_TOKEN or "").strip()
        return {"Authorization": "Bearer " + token} if token else {}

    def _log_error(self, filename, exc):
        """Трассировка ошибки обезличивания (для разбора сбоев).

        Включается переменной DOCX_ANON_ERROR_LOG=1 — по умолчанию молчим,
        чтобы не писать лишние файлы в проде.
        """
        if os.environ.get("DOCX_ANON_ERROR_LOG") not in ("1", "true", "yes"):
            return
        try:
            import traceback
            path = os.environ.get("DOCX_ANON_ERROR_LOG_PATH",
                                  "/tmp/docx-anon/error.log")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"=== {filename} ===\n")
                f.write(traceback.format_exc())
                f.write("\n")
        except Exception:
            pass

    # Значения, означающие «взять настройку сервиса».
    _AS_SERVICE = ("", "default", "как в сервисе", "настройка сервиса",
                   "как в документе", "из сервиса")
    _TRUE = ("включено", "1", "true", "on", "yes", "да", "вкл",
             "enable", "enabled", "маскировать", "вычищать", "вырезать")
    _FALSE = ("выключено", "0", "false", "off", "no", "нет", "выкл",
              "disable", "disabled", "не трогать", "оставлять")

    def _bool_valve(self, name: str):
        """Значение булева переключателя → True / False / None.

        None — «не задано»: берётся настройка сервиса. Принимаем подписи из
        интерфейса («Включено»/«Выключено»/«Не трогать»/«Маскировать» и т. п.)
        и технические значения (1/0/true/false/on/off/да/нет/вкл/выкл).
        Неизвестное значение — None: опечатка в настройке не должна ломать
        обезличивание.
        """
        raw = str(getattr(self.valves, name, "")).strip().lower()
        if raw in self._AS_SERVICE:
            return None
        if raw in self._TRUE:
            return True
        if raw in self._FALSE:
            return False
        return None

    def _text_valve(self, name: str):
        """Значение строкового переключателя → значение или None.

        «Как в сервисе»/«Как в документе» — не задано, сервис возьмёт своё.
        """
        raw = str(getattr(self.valves, name, "")).strip()
        if raw.lower() in self._AS_SERVICE:
            return None
        return raw or None

    def _commercial_flag(self):
        """Значение COMMERCIAL → True / False / None(=настройка сервиса)."""
        return self._bool_valve("COMMERCIAL")

    def _service_params(self):
        """Собирает параметры запроса к сервису из Valves.

        Каждый переключатель независим: не заданное значение не передаётся,
        и сервис берёт свою настройку по умолчанию.
        """
        params = {}

        # ── группа «Коммерческая информация» ──
        commercial = self._bool_valve("COMMERCIAL")
        if commercial is not None:
            params["commercial"] = "1" if commercial else "0"

        # ── режим замены ──
        mode = self._text_valve("MODE")
        if mode:
            # «Метки» → placeholder, «Звёздочки» → mask, «Удалить» → delete.
            mode_map = {
                "метки": "placeholder",
                "placeholder": "placeholder",
                "звёздочки": "mask",
                "звездочки": "mask",
                "mask": "mask",
                "удалить": "delete",
                "delete": "delete",
            }
            mapped = mode_map.get(mode.lower())
            if mapped:
                params["mode"] = mapped

        # ── нейросеть (NER) ──
        ner = self._bool_valve("NER_ENABLED")
        if ner is not None:
            params["nerEnabled"] = "1" if ner else "0"

        threshold = self._text_valve("NER_THRESHOLD")
        if threshold:
            try:
                value = float(threshold)
            except (TypeError, ValueError):
                value = None
            if value is not None and 0.0 <= value <= 1.0:
                params["nerThreshold"] = str(value)

        # ── детекторы ──
        gov = self._bool_valve("GOV_ENTITIES")
        if gov is not None:
            # «Не трогать» → True (ядро не трогает госорганы),
            # «Маскировать» → False (ядро маскирует наравне с остальными).
            params["govEntities"] = "1" if gov else "0"

        dict_names = self._bool_valve("DICT_NAMES")
        if dict_names is not None:
            params["dictNames"] = "1" if dict_names else "0"

        # ── метаданные и встроенные объекты ──
        keep_meta = self._bool_valve("KEEP_METADATA")
        if keep_meta is not None:
            params["keepMetadata"] = "1" if keep_meta else "0"

        strip = self._bool_valve("STRIP_EMBEDDED")
        if strip is not None:
            params["stripEmbedded"] = "1" if strip else "0"

        # ── языки ──
        ph_lang = self._text_valve("PLACEHOLDER_LANGUAGE")
        if ph_lang:
            params["placeholderLanguage"] = ph_lang

        lang = self._text_valve("LANGUAGE")
        if lang:
            params["language"] = lang

        return params

    def _anonymize(self, raw: bytes, filename: str):
        files = {"file": (filename or "document.bin", raw)}
        params = self._service_params()
        r = requests.post(
            self._service("/anonymize"),
            files=files,
            params=params,
            headers=self._auth(),
            timeout=self.valves.TIMEOUT,
        )
        if r.status_code != 200:
            raise RuntimeError(f"anonymize HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    def _extract(self, raw: bytes, filename: str):
        """Извлечение текста из офисного формата силами сервиса.

        Сервис умеет xlsx/pptx/odt/doc своими парсерами (без Pandoc и
        LibreOffice). Open WebUI тоже умеет xlsx/pptx/odt, но doc требует
        LibreOffice, а odt упирается в Pandoc. Поэтому сначала пробуем
        сервис, и только если он не смог — берём текст, извлечённый
        самим Open WebUI.
        """
        files = {"file": (filename or "document.bin", raw)}
        r = requests.post(
            self._service("/extract"),
            files=files,
            headers=self._auth(),
            timeout=self.valves.TIMEOUT,
        )
        if r.status_code != 200:
            return None
        try:
            data = r.json()
        except Exception:
            return None
        text = (data.get("text") or "").strip()
        return text or None

    def _restore(self, text: str, mapping: dict):
        r = requests.post(
            self._service("/restore"),
            json={"text": text, "map": mapping},
            headers=self._auth(),
            timeout=self.valves.TIMEOUT,
        )
        if r.status_code != 200:
            raise RuntimeError(f"restore HTTP {r.status_code}: {r.text[:200]}")
        return r.json().get("text", text)

    # ------------------------------------------------------------------
    # хуки
    # ------------------------------------------------------------------
    async def inlet(self, body: dict, __user__=None, __request__=None) -> dict:
        """Перехват до модели: обезличиваем вложения, оригиналы убираем.

        ВАЖНО про fail-closed: исключение из inlet НЕ является блокировкой.
        В Open WebUI v0.12.0 main.py:1700-1706 исключение при наличии
        session_id и chat_id проглатывается — запрос всё равно уходит к модели,
        только уже БЕЗ обезличивания (то есть с оригиналом). Поэтому при
        сбое мы не бросаем исключение, а:
          * удаляем оригинал из тела запроса (файл не доедет ни до RAG,
            ни до view_file, ни до file_context),
          * подставляем в сообщение явную плашку, что документ не обезличен
            и потому не отправлен.
        Так запрос физически не может унести ПДн.
        """
        items = self._file_items(body)
        if not items:
            return body

        # Периодически убираем старые карты замен (в них лежат исходные ПДн).
        self._cleanup_maps()

        anon_blocks = []
        blocked = []
        key = self._session_key(body)
        maps = {}
        kept_files = []  # не-документы (картинки и пр.) оставляем как есть

        for item in items:
            if not isinstance(item, dict):
                continue
            file_id = item.get("id")
            if not file_id:
                continue
            meta = item.get("meta") or {}
            filename, _fdata = await self._file_record(file_id)
            kind = self._classify(filename, meta)
            if kind == "media":
                # Картинка/аудио/видео — не документ. Пропускаем как есть:
                # обезличивать нечего, а блокировать обычный чат нельзя.
                kept_files.append(item)
                continue
            if kind == "extracted":
                # xlsx/pptx/odt/doc: байты файла ядро не читает, но текст
                # из этих файлов уходит в модель — значит обезличивать надо
                # именно его. Текст добываем в два шага:
                #   1) сервис разбирает файл своими парсерами (xlsx/pptx/odt/doc);
                #   2) если не вышло — берём текст, который Open WebUI уже
                #      извлёк и сложил в files.data['content'].
                raw, filename = await self._read_bytes(file_id)
                if not raw:
                    continue
                text_in = None
                try:
                    text_in = self._extract(raw, filename)
                except Exception:
                    text_in = None
                if not text_in:
                    text_in = await self._read_extracted_text(file_id)
                if not text_in:
                    # Текст не извлечён (разбор не завершён или пусто).
                    # Файл выпускать нельзя: его id остаётся в теле запроса,
                    # а по id модель достаёт оригинал через view_file.
                    # Значит — блокируем (fail-closed) и убираем id из тела.
                    if self.valves.FAIL_CLOSED:
                        blocked.append(f"{filename} (не удалось извлечь текст)")
                    else:
                        kept_files.append(item)
                    continue
                try:
                    res = self._anonymize(text_in.encode("utf-8"), filename + ".txt")
                except Exception as e:
                    if self.valves.FAIL_CLOSED:
                        blocked.append(f"{filename} ({str(e)[:120]})")
                    else:
                        kept_files.append(item)
                    continue
                anon_text = (res.get("text") or "").strip()
                if not anon_text:
                    if self.valves.FAIL_CLOSED:
                        blocked.append(f"{filename} (не удалось обезличить)")
                    else:
                        kept_files.append(item)
                    continue
                maps[file_id] = res.get("map")
                anon_blocks.append(
                    f'<anonymized_document name="{filename}" source="extracted">\n{anon_text}\n</anonymized_document>')
                await self._scrub_file_record(file_id, anon_text)
                continue
            if kind == "unsupported":
                # Формат, который не читает ни ядро, ни Open WebUI: обезличить
                # нечем, а ПДн внутри возможны → по политике не выпускаем.
                if self.valves.FAIL_CLOSED:
                    blocked.append(f"{filename} (формат не поддерживается обезличиванием)")
                else:
                    kept_files.append(item)
                continue
            # Класс document: читаем байты файла целиком — ядро обезличивает
            # сам документ (структуру, таблицы, колонтитулы).
            raw, filename = await self._read_bytes(file_id)
            if not raw:
                continue
            if len(raw) > self.valves.MAX_FILE_MB * 1024 * 1024:
                if self.valves.FAIL_CLOSED:
                    blocked.append(f"{filename} (больше {self.valves.MAX_FILE_MB} МБ)")
                else:
                    kept_files.append(item)
                continue
            try:
                res = self._anonymize(raw, filename)
            except Exception as e:
                self._log_error(filename, e)
                if self.valves.FAIL_CLOSED:
                    blocked.append(f"{filename} ({str(e)[:120]})")
                else:
                    kept_files.append(item)
                continue
            text = (res.get("text") or "").strip()
            if not text:
                if self.valves.FAIL_CLOSED:
                    blocked.append(f"{filename} (после обезличивания не осталось текста)")
                else:
                    kept_files.append(item)
                continue
            anon_blocks.append(f'<anonymized_document name="{filename}">\n{text}\n</anonymized_document>')
            maps[file_id] = res.get("map")
            # Затираем ПДн в записи файла: id остаётся в <attached_files>,
            # и по нему view_file достал бы оригинал.
            await self._scrub_file_record(file_id, text)

        # Плашка о заблокированных документах: пользователь должен понять,
        # почему модель «не видит» файл. Без неё запрос выглядит как пустой.
        if blocked:
            note = (
                "⚠️ Документ НЕ отправлен модели, потому что не удалось его обезличить: "
                + "; ".join(blocked)
                + ". Оригинал не покидает систему. Разберитесь с файлом и пришлите снова."
            )
            anon_blocks.append(f"<anonymization_notice>\n{note}\n</anonymization_notice>")

        # Оригиналы-документы убираем из тела ВСЕГДА, даже если нечего
        # подмешивать: иначе id документа с ПДн остаётся в запросе, и модель
        # достаёт оригинал через view_file. Пустой случай — это «всё
        # заблокировано», а не «ничего не делать».
        body["files"] = kept_files
        meta = body.get("metadata")
        if isinstance(meta, dict):
            meta["files"] = kept_files

        if not anon_blocks:
            # Ничего обезличивать не удалось и блокировать нечего (например,
            # только картинки) — убираем ссылки на файлы из сообщений, чтобы
            # оригиналы не достать через view_file.
            msgs0 = body.get("messages") or []
            if msgs0:
                last0 = msgs0[-1]
                content0 = last0.get("content")
                if isinstance(content0, str):
                    last0["content"] = _strip_attached_files(content0)
                elif isinstance(content0, list):
                    last0["content"] = _strip_attached_files_in_list(content0)
            return body

        # Карта — по сессии, чтобы outlet её нашёл (у outlet другое тело).
        self._save_map(key, {"files": maps, "created": time.time()})

        # Кладём обезличенный текст в последнее сообщение пользователя.
        msgs = body.get("messages") or []
        if msgs:
            last = msgs[-1]
            content = last.get("content")
            if isinstance(content, str):
                content = _strip_attached_files(content)
                last["content"] = content + "\n\n" + "\n\n".join(anon_blocks)
            elif isinstance(content, list):
                content = _strip_attached_files_in_list(content)
                content.append({"type": "text", "text": "\n\n".join(anon_blocks)})
            else:
                last["content"] = "\n\n".join(anon_blocks)
        else:
            body["messages"] = [{"role": "user", "content": "\n\n".join(anon_blocks)}]

        # Аудит: что РЕАЛЬНО уходит в модель. Файл внутри контейнера, 0600 —
        # для проверяющего: так видно, что ПДн не проходят транзитом.
        # Путь можно переопределить переменной DOCX_ANON_AUDIT.
        try:
            audit = os.environ.get("DOCX_ANON_AUDIT") or "/tmp/docx-anon/model_input.log"
            os.makedirs(os.path.dirname(audit), exist_ok=True)
            with open(audit, "a", encoding="utf-8") as _f:
                _f.write("\n=== inlet " + str(int(time.time())) + " ===\n")
                _f.write(body["messages"][-1].get("content") or "")
                _f.write("\n")
            os.chmod(audit, 0o600)
        except Exception:
            pass

        # body["files"] уже очищен выше (до подмешивания) — здесь только
        # страховка на случай, если появится новый код после этой точки.
        body["files"] = kept_files
        meta = body.get("metadata")
        if isinstance(meta, dict):
            meta["files"] = kept_files

        return body

    @staticmethod
    def _output_text(output) -> str:
        """Весь текст из структурированных блоков ответа (output)."""
        if not isinstance(output, list):
            return ""
        parts = []
        for item in output:
            if not isinstance(item, dict):
                continue
            for sub in (item.get("content") or []):
                if isinstance(sub, dict) and sub.get("text"):
                    parts.append(str(sub["text"]))
        return " ".join(parts)

    def _may_contain_marks(self, content: str, maps: dict) -> bool:
        """Есть ли в тексте хоть одна метка — со скобками или без.

        Модель часто переписывает метку («**ФИО_1**», «ФИО_1»), поэтому
        проверять только «[» нельзя: так ушёл бы ответ с восстановленными
        значениями. Но и проверять только голые метки нельзя — они короткие
        и встречаются в обычном тексте («счёт 1»).
        """
        if "[" in content:
            return True
        # Голые метки: ищем по префиксу категории из карты (ФИО_1, ПАСПОРТ_1…),
        # причём с границей по слову, чтобы «счёт 1» не считалось меткой.
        # В placeholder категории может быть префикс: «ООО «[КОМПАНИЯ_1]»» —
        # берём внутреннюю метку, иначе по ней ничего не найдётся.
        for mapping in maps.values():
            for entry in ((mapping or {}).get("entries") or []):
                ph = str(entry.get("placeholder") or "")
                inner = re.search(r"\[([^\[\]]+)\]", ph)
                bare = inner.group(1) if inner else ph.strip("[]")
                if not bare:
                    continue
                if re.search(r"(?<![\w])" + re.escape(bare) + r"(?![\w])", content):
                    return True
        return False

    async def outlet(self, body: dict, __user__=None, __request__=None) -> dict:
        """Обратный проход: возвращаем настоящие значения в ответ модели."""
        key = self._session_key(body)
        saved = self._load_map(key)
        if not saved:
            return body
        maps = saved.get("files") or {}
        if not maps:
            return body

        msgs = body.get("messages") or []
        if not msgs:
            return body
        # Ответ модели — последнее сообщение с ролью assistant. Брать msgs[-1]
        # нельзя: в теле outlet сообщение пользователя может идти после
        # ответа (или ответ — не последнее), и тогда восстановление не сработает.
        target = None
        for m in reversed(msgs):
            if isinstance(m, dict) and m.get("role") == "assistant":
                target = m
                break
        if target is None:
            return body
        content = target.get("content")
        if not isinstance(content, str) or len(content) > self.valves.MAX_RESTORE_CHARS:
            return body
        # Поле output — структурированные блоки ответа (reasoning/output_text).
        # UI рисует именно их, а не content: без восстановления здесь пользователь
        # видит метки, хотя content уже восстановлен. Поэтому ранний выход
        # смотрит и на content, и на output.
        output = target.get("output")
        if not self._may_contain_marks(content, maps) and not self._may_contain_marks(
            self._output_text(output), maps
        ):
            return body

        # Каждая карта — свой набор меток. Восстанавливает сервис (там ядро
        # со склонением — «его супруга [ФИО_3]» → «Белоусова»).
        for file_id, mapping in maps.items():
            if not mapping:
                continue
            try:
                content = self._restore(content, mapping)
            except Exception:
                # Не восстановили — оставляем метки: безопаснее, чем неверные значения.
                continue

        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                chunks = item.get("content")
                if not isinstance(chunks, list):
                    continue
                for ch in chunks:
                    if isinstance(ch, dict) and isinstance(ch.get("text"), str):
                        try:
                            ch["text"] = self._restore(ch["text"], list(maps.values())[0])
                        except Exception:
                            continue

        target["content"] = content
        return body
