# -*- coding: utf-8 -*-
"""Мок-модули Open WebUI для теста фильтра (v0.12.0).

Минимально то, что использует references/openwebui-filter.py:
  open_webui.models.files.Files.get_file_by_id   (async)
  open_webui.storage.provider.Storage.get_file
"""
import os
from pathlib import Path

_ROOT = Path(os.environ.get("MOCK_FILES_ROOT", "/tmp/mockfiles"))


class FileModel:
    def __init__(self, id, filename, path, data=None, meta=None, user_id="u1"):
        self.id = id
        self.filename = filename
        self.path = path
        self.data = data or {}
        self.meta = meta or {}
        self.user_id = user_id


class Files:
    @staticmethod
    async def get_file_by_id(id):
        # id вида 'f:<имя файла>'
        if not id.startswith('f:'):
            return None
        p = _ROOT / id[2:]
        if not p.is_file():
            return None
        # Если фильтр уже затёр запись — отдаём затёртое содержимое:
        # так тест видит, что оригинал по этому id больше недоступен.
        rec = _RECORDS.get(id)
        data = dict(rec['data']) if rec else {'content': ''}
        return FileModel(id, p.name, str(p), data=data, meta={})

    @staticmethod
    async def update_file_data_by_id(id, data, db=None):
        """Затирает содержимое записи файла (в боевом Open WebUI — обезличенным текстом).

        В моке: держим состояние в памяти, чтобы тест мог проверить, что
        оригинальный текст ПДн больше недоступен по id файла.
        """
        rec = _RECORDS.get(id)
        if rec is None:
            # файла нет — создаём запись (как в боевой системе после загрузки)
            rec = {'data': {'status': 'completed', 'content': ''}}
            _RECORDS[id] = rec
        rec['data'] = {**rec.get('data', {}), **data}
        return rec


# Записи файлов, «затёртые» фильтром: id -> {'data': {...}}
_RECORDS = {}
