#!/usr/bin/env python3
"""Живая проверка пользовательских параметров (Filter.UserValves).

Запускается ВНУТРИ контейнера open-webui:

    docker exec app_4696b1a1_open-webui python3 /tmp/test_user_valves_live.py

Проверяется полный цикл на живой Open WebUI:

  1. /valves/user/spec отдаёт 33 поля (панель «Функции» покажет параметры).
  2. Сохранение одного override → в БД только это поле.
  3. Override реально применяется: документ уходит к модели обезличенным
     ИНАЧЕ, чем при глобальной настройке.
  4. «Как у администратора» → действует глобальная настройка.
  5. Сброс override → поведение возвращается к глобальному.
  6. Обезличивание работает на всех этапах (ФИО замаскировано всегда).

Аудит-лог фильтра (/tmp/docx-anon/model_input.log) показывает, что РЕАЛЬНО
ушло к модели, — по нему и проверяем.
"""

import json
import os
import subprocess
import sys
import time
import uuid

BASE = os.environ.get("OWUI_BASE", "http://127.0.0.1:8080")
MODEL = os.environ.get("OWUI_MODEL", "MiniMax-M3.1-Flash")
FILTER_ID = "docx_anonymizer"
AUDIT = "/tmp/docx-anon/model_input.log"
TMP = "/tmp/_uv"

KEY = open("/app/backend/data/.deploy/owui_key").read().strip()

_passed = 0
_failed = 0


def ok(cond, what, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"[OK  ] {what}")
    else:
        _failed += 1
        print(f"[FAIL] {what} {detail}")


def api(method, path, data=None):
    import urllib.request
    body = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
    req = urllib.request.Request(
        BASE + path, data=body, method=method,
        headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode()
        return r.status, (json.loads(raw) if raw else None)


def curl(args):
    return subprocess.run(["curl", "-s", *args], capture_output=True, text=True, timeout=300)


DOCX_TEXT = (
    "Договор поставки № 77/2024\n"
    'Контрагент: ООО "Ромашка-Авто", ИНН 7701234567.\n'
    "Ответственный: Иванов Иван Иванович, тел. +7 (495) 123-45-67.\n"
)
PERSON = "Иванов Иван Иванович"
COMPANY = "РОМАШКА"


def make_docx(path):
    """Минимальный .docx с текстом (zip + OOXML)."""
    import zipfile
    doc = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        + "".join(
            "<w:p><w:r><w:t>" + line.replace("&", "&amp;").replace("<", "&lt;") + "</w:t></w:r></w:p>"
            for line in DOCX_TEXT.split("\n")
        )
        + "</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                   "</Types>")
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                   "</Relationships>")
        z.writestr("word/document.xml", doc)
    return path


def audit_size():
    try:
        return os.path.getsize(AUDIT)
    except Exception:
        return 0


def audit_since(offset):
    """Часть аудит-лога после заданного смещения."""
    try:
        with open(AUDIT, "rb") as f:
            f.seek(offset)
            return f.read().decode("utf-8", "ignore")
    except Exception:
        return ""


def send_doc(tag):
    """Загружает документ и отправляет его в чат. Возвращает (аудит, ошибка)."""
    os.makedirs(TMP, exist_ok=True)
    path = make_docx(f"{TMP}/uv_{tag}.docx")

    up = curl(["-X", "POST", "-H", f"Authorization: Bearer {KEY}",
               "-F", f"file=@{path}", f"{BASE}/api/v1/files/"])
    try:
        fid = json.loads(up.stdout)["id"]
    except Exception as exc:
        return None, f"загрузка не удалась: {up.stdout[:200]} {exc}"

    files = [{"type": "file", "id": fid, "name": os.path.basename(path),
              "meta": {"name": os.path.basename(path)}}]
    open(f"{TMP}/chat.json", "w").write(json.dumps(
        {"chat": {"title": tag, "models": [MODEL], "files": files}}, ensure_ascii=False))
    cr = curl(["-X", "POST", "-H", f"Authorization: Bearer {KEY}",
               "-H", "Content-Type: application/json",
               "--data-binary", f"@{TMP}/chat.json", f"{BASE}/api/v1/chats/new"])
    try:
        cid = json.loads(cr.stdout)["id"]
    except Exception as exc:
        return None, f"чат не создан: {cr.stdout[:200]} {exc}"

    uid = str(uuid.uuid4())
    body = {
        "model": MODEL, "stream": False,
        "messages": [{"id": uid, "role": "user",
                      "content": "Назови все ФИО, паспорта, ИНН, телефоны и компании из документа."}],
        "files": files, "chat_id": cid,
        "metadata": {"user_message_id": uid, "chat_id": cid, "session_id": cid},
        "message_ids": [{"model_id": MODEL, "message_id": str(uuid.uuid4())}],
        "id": uid,
    }
    open(f"{TMP}/req.json", "w").write(json.dumps(body, ensure_ascii=False))
    offset = audit_size()
    curl(["-X", "POST", "-H", f"Authorization: Bearer {KEY}",
          "-H", "Content-Type: application/json",
          "--data-binary", f"@{TMP}/req.json", f"{BASE}/api/v1/chat/completions"])
    time.sleep(2)
    return audit_since(offset), None


def company_masked(text):
    """Компания замаскирована: исходного названия нет."""
    return COMPANY not in text.upper()


def anonymized(text):
    """Обезличивание реально сработало: в аудите есть метки и нет ПДн.

    Без этой проверки уведомление об ошибке («не удалось обезличить»)
    выглядело бы как «название компании не найдено» — то есть как успех.
    """
    return ("<anonymized_document" in text
            and ("[КОМПАНИЯ" in text.upper() or "[ФИО" in text.upper()
                 or "[ОРГ" in text.upper() or "[ДОГОВОР" in text.upper()))


def main():
    # ── 1. схема пользовательских параметров ───────────────────────────
    st, us = api("GET", f"/api/v1/functions/id/{FILTER_ID}/valves/user/spec")
    ok(us is not None, "пользовательская схема отдаётся (не null)")
    if us:
        props = us.get("properties", {})
        ok(len(props) == 33, f"в пользовательской схеме 33 поля (получено {len(props)})")
        ok(props["MODE"]["input"]["options"][0]["value"] == "Как у администратора",
           "первая опция MODE — «Как у администратора»")

    st, gs = api("GET", f"/api/v1/functions/id/{FILTER_ID}/valves/spec")
    ok(len(gs.get("properties", {})) == 33, "глобальная схема не сломалась (33 поля)")

    # ── 2. запоминаем глобальные настройки и задаём свои ────────────────
    st, before_global = api("GET", f"/api/v1/functions/id/{FILTER_ID}/valves")
    before_global = dict(before_global or {})
    # /valves/update ЗАМЕНЯЕТ объект целиком: мержим, иначе затрём
    # SERVICE_TOKEN/SERVICE_URL и сервис начнёт отвечать 401.
    def set_global(**kw):
        merged = dict(before_global)
        merged.update(kw)
        st, res = api("POST", f"/api/v1/functions/id/{FILTER_ID}/valves/update", merged)
        before_global.clear()
        before_global.update(merged)
        return st, res

    st, _ = set_global(CATEGORY_COMPANY="Маскировать", MODE="Метки", COMMERCIAL="Включено")
    ok(st == 200, "глобальные настройки обновлены (с мержем)")
    time.sleep(1)

    # ── 3. override пользователя: компанию НЕ маскировать ──────────────
    st, saved = api("POST", f"/api/v1/functions/id/{FILTER_ID}/valves/user/update",
                    {"CATEGORY_COMPANY": "Не маскировать"})
    ok(st == 200, "override сохранён")
    st, got = api("GET", f"/api/v1/functions/id/{FILTER_ID}/valves/user")
    ok(got is not None and got.get("CATEGORY_COMPANY") == "Не маскировать",
       f"override прочитан обратно: {got}")

    # ── 4. override реально применяется ────────────────────────────────
    audit, err = send_doc("uv-override")
    ok(err is None, "документ отправлен (override)", str(err or ""))
    if audit:
        ok(anonymized(audit), "обезличивание сработало (документ дошёл до модели метками)",
           f"(аудит: {audit[-220:]!r})")
        ok(COMPANY in audit.upper(),
           "пользовательский override применён: компания НЕ замаскирована",
           f"(аудит: {audit[-220:]!r})")
        ok(PERSON not in audit,
           "ФИО всё равно замаскировано (override касался только компании)",
           f"(аудит: {audit[-220:]!r})")
    else:
        ok(False, "override применён: нет записи аудита", f"(err={err})")

    # ── 5. «Как у администратора» → действует глобальная настройка ──────
    api("POST", f"/api/v1/functions/id/{FILTER_ID}/valves/user/update",
        {"CATEGORY_COMPANY": "Как у администратора"})
    audit, err = send_doc("uv-inherit")
    ok(err is None, "документ отправлен (наследование)", str(err or ""))
    if audit:
        ok(anonymized(audit), "обезличивание сработало при наследовании",
           f"(аудит: {audit[-220:]!r})")
        ok(company_masked(audit),
           "«Как у администратора» → действует глобальная настройка (компания замаскирована)",
           f"(аудит: {audit[-220:]!r})")
        ok(PERSON not in audit, "ФИО замаскировано при наследовании")
    else:
        ok(False, "наследование: нет записи аудита", f"(err={err})")

    # ── 6. полный сброс override ───────────────────────────────────────
    api("POST", f"/api/v1/functions/id/{FILTER_ID}/valves/user/update", {})
    st, got = api("GET", f"/api/v1/functions/id/{FILTER_ID}/valves/user")
    ok(got in (None, {}), f"override сброшен: {got}")

    audit, err = send_doc("uv-reset")
    if audit:
        ok(anonymized(audit), "обезличивание сработало после сброса",
           f"(аудит: {audit[-220:]!r})")
        ok(company_masked(audit), "после сброса действует глобальная настройка")
        ok(PERSON not in audit, "обезличивание работает и после сброса")
    else:
        ok(False, "после сброса нет записи аудита", f"(err={err})")

    # ── 7. возвращаем глобальные настройки как были ────────────────────
    if before_global:
        api("POST", f"/api/v1/functions/id/{FILTER_ID}/valves/update", before_global)
        st, chk = api("GET", f"/api/v1/functions/id/{FILTER_ID}/valves")
        ok(len(chk.get("SERVICE_TOKEN") or "") > 0 and chk.get("SERVICE_URL"),
           "глобальные настройки восстановлены вместе с SERVICE_*/токеном")
    api("POST", f"/api/v1/functions/id/{FILTER_ID}/valves/user/update", {})

    print(f"\nИТОГ: {_passed}/{_passed + _failed}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
