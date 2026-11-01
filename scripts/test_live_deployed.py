#!/usr/bin/env python3
"""Живая проверка развёрнутого фильтра Open WebUI (end-to-end).

Запускается ВНУТРИ контейнера open-webui (нужен доступ к его БД и HTTP):

    docker exec app_4696b1a1_open-webui python3 /path/to/test_live_deployed.py

Проверяет сквозной сценарий на настоящем стенде:
  L1 DOCX с ПДн  → модель получила метки (0 ПДн), в чате — восстановленные значения
  L2 PDF с ПДн   → то же самое
  L3 битый docx  → документ НЕ выпущен к модели, пользователь предупреждён (fail-closed)
  L4 .xlsx       → документ НЕ выпущен (формат не обезличивается)
  L5 картинка    → чат работает, картинка не ломает пайплайн
  L6 без вложений → обычный чат работает

Критерий fail-closed: содержимое оригинала не должно попасть НИ в сообщения
пользователя, НИ в ответ модели. Наличие плашки «документ не передан» — хорошо.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from live_fixtures import make_docx as _make_docx, make_pdf as _make_pdf  # noqa: E402

KEY = os.environ.get("OWUI_API_KEY", "")
if not KEY:
    # Ключ можно положить файлом: /tmp/owui_key (одна строка).
    # Через -e значение может потеряться на стороне оркестратора.
    p = "/tmp/owui_key"
    if os.path.isfile(p):
        KEY = open(p).read().strip()
BASE = os.environ.get("OWUI_BASE", "http://127.0.0.1:8080")
MODEL = os.environ.get("OWUI_MODEL", "MiniMax-M3.1-Flash")
DB = os.environ.get("OWUI_DB", "/app/backend/data/webui.db")

PROBES = ["Белоусов", "Гусейнова", "482913", "Молодогвардейская", "Тольятти", "443010"]
MARK_RE = re.compile(r"\[[А-ЯЁ][А-ЯЁ_]*_\d+\]")

results = []


def curl(args, **kw):
    return subprocess.run(["curl", "-s", *args], capture_output=True, text=True, timeout=300, **kw)


def chat_db(chat_id):
    con = sqlite3.connect(DB)
    row = con.execute("select chat from chat where id=?", (chat_id,)).fetchone()
    con.close()
    return json.loads(row[0]) if row else {}


def ask(files, tag):
    """Создать чат с вложениями и отправить сообщение. Возвращает (chat_id, ответ)."""
    cb = {"chat": {"title": tag, "models": [MODEL], "files": files}}
    open("/tmp/_lc_c.json", "w").write(json.dumps(cb, ensure_ascii=False))
    cr = curl(["-X", "POST", "-H", f"Authorization: Bearer {KEY}", "-H", "Content-Type: application/json",
               "--data-binary", "@/tmp/_lc_c.json", f"{BASE}/api/v1/chats/new"])
    chat_id = json.loads(cr.stdout)["id"]
    uid = str(uuid.uuid4())
    mb = {
        "model": MODEL, "stream": False,
        "messages": [{"id": uid, "role": "user",
                      "content": "Перечисли все ФИО и паспортные данные из приложенного документа."}],
        "files": files, "chat_id": chat_id,
        "metadata": {"user_message_id": uid, "chat_id": chat_id, "session_id": chat_id},
        "message_ids": [{"model_id": MODEL, "message_id": str(uuid.uuid4())}],
        "id": uid,
    }
    open("/tmp/_lc_r.json", "w").write(json.dumps(mb, ensure_ascii=False))
    raw = curl(["-X", "POST", "-H", f"Authorization: Bearer {KEY}", "-H", "Content-Type: application/json",
                "--data-binary", "@/tmp/_lc_r.json", f"{BASE}/api/v1/chat/completions"]).stdout
    return chat_id, raw


def upload(path):
    r = curl(["-X", "POST", "-H", f"Authorization: Bearer {KEY}", "-F", f"file=@{path}",
              f"{BASE}/api/v1/files/"])
    try:
        d = json.loads(r.stdout)
    except Exception:
        return None
    return d if d.get("id") else None


def check_doc(tag, path, probes):
    """Документ обезличивается: в модели — метки, в чате — настоящие значения."""
    fi = upload(path)
    if not fi:
        results.append((tag, False, "файл не загрузился"))
        return
    files = [{"type": "file", "id": fi["id"], "name": fi.get("filename"), "meta": fi.get("meta")}]
    chat_id, _ = ask(files, tag)
    msgs = (chat_db(chat_id).get("history") or {}).get("messages", {})
    assistant = [m for m in msgs.values() if m.get("role") == "assistant"]
    if not assistant:
        results.append((tag, False, "в чате нет ответа ассистента"))
        return
    orig = str(assistant[0].get("originalContent") or "")
    final = str(assistant[0].get("content") or "")
    orig_leaks = [p for p in probes if p in orig]
    final_leaks = [p for p in probes if p in final]
    marks_orig = MARK_RE.findall(orig)
    marks_final = MARK_RE.findall(final)
    ok = (not orig_leaks) and bool(marks_orig) and (not marks_final)
    detail = (f"до outlet: ПДн={orig_leaks or 'нет'} меток={len(marks_orig)}; "
              f"после: ПДн={final_leaks or 'нет'} меток={len(marks_final)}")
    results.append((tag, ok, detail))


def check_blocked(tag, path, marker):
    """Документ не должен уйти к модели: ни содержимого, ни в запросе, ни в ответе."""
    fi = upload(path)
    if not fi:
        results.append((tag, False, "файл не загрузился"))
        return
    files = [{"type": "file", "id": fi["id"], "name": fi.get("filename"), "meta": fi.get("meta")}]
    chat_id, _ = ask(files, tag)
    msgs = (chat_db(chat_id).get("history") or {}).get("messages", {})
    user_text = " ".join(str(m.get("content") or "") for m in msgs.values() if m.get("role") == "user")
    asst_text = " ".join(str(m.get("content") or "") for m in msgs.values() if m.get("role") == "assistant")
    leaked = marker in user_text or marker in asst_text
    # Предупреждение пользователю: модель перефразирует плашку как угодно
    # («не вижу документа», «не был передан», «не удалось обезличить»…),
    # поэтому ищем СМЫСЛ по набору маркеров без учёта регистра, а не
    # конкретную формулировку. Критерий устойчив к перефразировке.
    low = asst_text.lower()
    notice_markers = [
        "не был передан", "не передан", "не удалось", "не отправлен",
        "не вижу", "не видел", "не вижу прикреплённого", "не вижу прикрепленного",
        # «не был прикреплён» — с «был» и без: модель перефразирует плашку,
        # поэтому держим оба варианта написания.
        "не прикреплён", "не прикреплен", "не был прикреплён", "не был прикреплен",
        "не была прикреплена", "не было прикреплено", "не прикрепил",
        "не прошёл проверку", "не прошел проверку",
        "без содержимого", "нет содержимого", "формат не поддерживается",
        "обезличиван", "не могу прочитать", "не получилось",
        "не распознан", "неподдерживаемого формата", "не поддерживается",
    ]
    notice = any(m in low for m in notice_markers)
    ok = (not leaked) and notice
    results.append((tag, ok, f"оригинал в сообщениях: {'ДА' if leaked else 'нет'}; "
                             f"предупреждение: {'да' if notice else 'НЕТ'}"))


def check_plain(tag):
    """Обычный чат без вложений должен работать и не содержать меток."""
    chat_id, raw = ask([], tag)
    try:
        txt = json.loads(raw)["choices"][0]["message"]["content"]
    except Exception:
        results.append((tag, False, "не разобрали ответ"))
        return
    marks = MARK_RE.findall(txt)
    results.append((tag, not marks, f"ответ получен, меток={len(marks)}"))


def main():
    if not KEY:
        print("Нужен OWUI_API_KEY (admin API-ключ Open WebUI)")
        return 2

    docx = "/tmp/live_check.docx"
    pdf = "/tmp/live_check.pdf"
    # Фикстуры готовим сами и всегда перезаписываем: проверка не должна
    # зависеть от того, что кто-то заранее положил в /tmp (и не должна
    # цеплять битый файл от прошлого прогона).
    docx = _make_docx(docx)
    pdf = _make_pdf(pdf)
    check_doc("L1 DOCX с ПДн", docx, PROBES)
    check_doc("L2 PDF с ПДн", pdf, ["Кузнецов", "Белова", "772816897110"])

    # L3: битый docx
    with open("/tmp/live_broken.docx", "w", encoding="utf-8") as f:
        f.write("not a zip at all\n")
    check_blocked("L3 битый docx (fail-closed)", "/tmp/live_broken.docx", "not a zip at all")

    # L4: xlsx
    with open("/tmp/live_book.xlsx", "wb") as f:
        f.write(b"PK\x03\x04" + b"\x00" * 40)
    check_blocked("L4 .xlsx (формат не обезличивается)", "/tmp/live_book.xlsx", "PK")

    # L5: картинка — обычный чат не должен ломаться
    with open("/tmp/live_pic.png", "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)
    fi = upload("/tmp/live_pic.png")
    if fi:
        chat_id, raw = ask([{"type": "file", "id": fi["id"], "name": fi.get("filename"),
                             "meta": fi.get("meta")}], "L5 картинка")
        ok = '"choices"' in raw and "error" not in raw[:60].lower()
        results.append(("L5 картинка (обычный чат не ломается)", ok, "ответ получен" if ok else raw[:120]))
    else:
        results.append(("L5 картинка", False, "файл не загрузился"))

    # L6: без вложений
    check_plain("L6 чат без вложений")

    print("=" * 78)
    print("ЖИВАЯ ПРОВЕРКА ФИЛЬТРА OPEN WEBUI")
    print("=" * 78)
    for name, ok, detail in results:
        print(f"[{'OK  ' if ok else 'FAIL'}] {name}")
        print(f"        {detail}")
    passed = sum(1 for _, ok, _ in results if ok)
    print("-" * 78)
    print(f"ИТОГ: {passed}/{len(results)}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
