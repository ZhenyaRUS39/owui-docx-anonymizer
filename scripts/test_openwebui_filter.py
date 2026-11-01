#!/usr/bin/env python3
"""Тест Open WebUI-фильтра docx-anonymizer на 10 запросах.

Проверяет сквозной путь: Filter.inlet() → обезличивание (через HTTP-сервис) →
тело запроса, которое увидит модель → Filter.outlet() → восстановленный ответ.

Что проверяется для каждого случая:
  1. inlet отработал без исключения и модель получила текст документа;
  2. в теле запроса НЕТ ни одного исходного значения из карты замен (утечек нет);
  3. `files` вычищены из body, body["metadata"] и сообщений (иначе модель получит
     оригинал через RAG, view_file или file_context);
  4. outlet восстановил метку из карты в настоящее значение.

Фильтр ходит в локальный HTTP-сервис обезличивания (references/anon_service.mjs) —
поднимаем его на свободном порту. Модель Open WebUI замокана (scripts/owui_mock).

Запуск:
  python3 scripts/test_openwebui_filter.py
"""
import argparse
import asyncio
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import find_repo, find_fixtures, find_model  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "owui_mock"))


def load_filter_module():
    spec = importlib.util.spec_from_file_location(
        "owui_filter", HERE.parent / "references" / "openwebui-filter.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def start_service(work: Path, repo: str, ner: str):
    """Поднимает локальный HTTP-сервис обезличивания. Возвращает (port, proc)."""
    svc_src = HERE.parent / "references" / "anon_service.mjs"
    if not svc_src.is_file():
        return None, None
    with socket.socket() as s_:
        s_.bind(("127.0.0.1", 0))
        port = s_.getsockname()[1]

    env = dict(os.environ)
    env.update({
        "DOCX_ANON_REPO": repo,
        "DOCX_ANON_WRAPPER": str(HERE / "anonymize.mjs"),
        "DOCX_ANON_WORKDIR": str(work / "svc-maps"),
        "DOCX_ANON_PORT": str(port),
        "DOCX_ANON_HOST": "127.0.0.1",
    })
    if ner:
        env["DOCX_ANON_NER"] = ner
    else:
        env.pop("DOCX_ANON_NER", None)

    proc = subprocess.Popen(["node", str(svc_src)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for _ in range(80):
        time.sleep(0.25)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                if r.status == 200:
                    return port, proc
        except Exception:
            if proc.poll() is not None:
                break
    proc.terminate()
    return None, None


def root_token(text: str) -> str:
    """Корень имени без падежных окончаний — для сравнения восстановления.

    Ядро хранит один канон («Фомин Денис») и склоняет его по контексту,
    поэтому в ответе фамилия может стоять в другом падеже («Денису Фомину»).
    Сравниваем по основе: первые 4-5 букв каждого слова.
    """
    if not text:
        return ""
    parts = re.findall(r"[А-ЯЁA-Z][а-яёa-z]+", text)
    return " ".join(p[:4].lower() for p in parts)


def collect_cases(fx_root: Path):
    cases = []
    for p in sorted((fx_root / "heldout").glob("*/*.docx")):
        if not p.is_file():
            continue
        gold = p.with_suffix("").with_suffix(".gold.json")
        if not gold.is_file():
            continue
        data = json.loads(gold.read_text(encoding="utf-8"))
        ents = data.get("entities") if isinstance(data, dict) else data
        vals = []
        for e in ents or []:
            if isinstance(e, dict) and e.get("value"):
                vals.append(e["value"])
        cases.append((p.parent.name + "/" + p.stem, p, vals))
    return cases[:10]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=find_repo())
    ap.add_argument("--fixtures", default=find_fixtures())
    ap.add_argument("--script", default=str(HERE / "anonymize.mjs"))
    ap.add_argument("--ner", default=find_model(), help="каталог модели NER (без него полнота ниже)")
    args = ap.parse_args()

    cases = collect_cases(Path(args.fixtures))
    if not cases:
        print("НЕТ ФИКСТУР — нечего тестировать")
        return 2

    work = Path(tempfile.mkdtemp(prefix="owui-filter-test-"))
    mock_files = work / "mockfiles"
    mock_files.mkdir(parents=True)
    os.environ["MOCK_FILES_ROOT"] = str(mock_files)

    port, svc = start_service(work, args.repo, args.ner)
    if svc is None:
        print("НЕ УДАЛОСЬ ПОДНЯТЬ СЕРВИС ОБЕЗЛИЧИВАНИЯ")
        return 3

    mod = load_filter_module()

    # Бэкенд Open WebUI пересоздаёт self.valves перед каждым вызовом
    # (utils/plugin.py:64-70) из значений в БД; их там нет → остаются дефолты
    # класса. В pydantic v2 model_fields[...].default не действует — нужен
    # подкласс с нужными дефолтами.
    class _TestValves(mod.Filter.Valves):
        SERVICE_URL: str = f"http://127.0.0.1:{port}"
        WORKDIR: str = str(work / "maps")

    mod.Filter.Valves = _TestValves
    flt = mod.Filter()
    flt.valves = _TestValves()

    results = []
    for idx, (name, docx, values) in enumerate(cases, 1):
        fid = "f:" + docx.name
        shutil.copyfile(docx, mock_files / docx.name)
        req = {
            "model": "test-model",
            "messages": [
                {"role": "system", "content": "Ты помощник."},
                {"role": "user", "content": "Разбери документ и найди подвох."},
            ],
            "files": [{"id": fid, "name": docx.name, "type": "file"}],
            "metadata": {
                "files": [{"id": fid, "name": docx.name, "type": "file"}],
                "chat_id": f"chat-{idx}",
                "message_id": f"msg-{idx}",
                "session_id": "sess-1",
            },
        }
        try:
            out = asyncio.run(flt.inlet(req))
        except Exception as e:  # noqa: BLE001
            results.append((f"{idx}. {name}", False, f"inlet: {e}"))
            continue

        text = out["messages"][-1].get("content", "")
        if isinstance(text, list):
            text = " ".join(p.get("text", "") for p in text if isinstance(p, dict))

        blob = json.dumps(out, ensure_ascii=False, default=str)
        leaks = [v for v in values if v in blob]
        files_left = bool(out.get("files")) or bool(out.get("metadata", {}).get("files"))
        msg_files = any(m.get("files") for m in out.get("messages", []))

        mp = Path(flt.valves.WORKDIR) / (flt._session_key(req) + ".json")
        restored_ok = False
        if mp.exists():
            saved = json.loads(mp.read_text(encoding="utf-8"))
            entries = []
            for m in (saved.get("files") or {}).values():
                entries.extend((m or {}).get("entries", []))
            if entries:
                token = entries[0].get("placeholder") or entries[0].get("token")
                original = entries[0].get("original") or entries[0].get("value") or ""
                ans = {"id": "a1", "role": "assistant", "content": f"Проверка: {token}."}
                res = asyncio.run(flt.outlet({"messages": [ans],
                                              "chat_id": req["metadata"]["chat_id"]}))
                got = res["messages"][0]["content"]
                restored_ok = token not in got
                if restored_ok and original:
                    # Ядро восстанавливает КАНОН и склоняет его по контексту
                    # («Денису Фомину» в тексте → «Фомин Денис»), поэтому
                    # сравниваем по корню токенов, а не по точной строке.
                    root = root_token(original)
                    restored_ok = root in root_token(got) or any(
                        root_token(v) and root_token(v) in root_token(got)
                        for v in (entries[0].get("variants") or [])
                    )

        ok = (not leaks) and (not files_left) and (not msg_files) and restored_ok \
            and len(text.strip()) > 50
        detail = (f"сущностей={len(values)}, модель получила {len(text)} симв. "
                  f"(меток {len(re.findall(r'.ФИО_', text))}), "
                  f"утечек={len(leaks)}, файлы вычищены={not files_left and not msg_files}, "
                  f"outlet восстановил={restored_ok}")
        results.append((f"{idx}. {name}", ok, detail))

    svc.terminate()
    print("=" * 88)
    print("ТЕСТ Open WebUI-ФИЛЬТРА: %d кейсов" % len(results))
    print("=" * 88)
    for name, ok, detail in results:
        print(f"[{'OK  ' if ok else 'FAIL'}] {name}")
        print(f"        {detail}")
    passed = sum(1 for _, ok, _ in results if ok)
    print("-" * 88)
    print(f"ИТОГ: {passed}/{len(results)}")
    print(f"рабочие каталоги: {work}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
