#!/usr/bin/env python3
"""Тест-раннер скилла docx-anonymizer: 10 кейсов, проверка всего пути.

Каждый кейс — документ с gold-разметкой. Проверяется 8 утверждений:

  CLI (scripts/anonymize.mjs):
  A1 обезличивание отработало без ошибок
  A2 утечек в обезличенном файле нет (метод upstream: поиск по всем XML-частям)
  A3 восстановление по карте отработало, unresolved == 0
  A4 после восстановления вернулись ВСЕ gold-значения (полнота обратного прохода)

  Open WebUI-фильтр (references/openwebui-filter.py):
  B1 inlet отработал без ошибок
  B2 текст, который увидит модель, читаемый (не бинарный мусор) и непустой
  B3 в теле запроса нет ни одного исходного значения (утечек нет)
  B4 files вычищены из body, metadata и сообщений
  B5 outlet восстановил метку из карты в настоящее значение

Запуск:
  python3 test_pipeline.py --repo /opt/docx-anonymizer --ner /opt/docx-anon-model
  python3 test_pipeline.py --repo ... --no-ner     (без модели — полнота ниже)
"""
import argparse
import asyncio
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "owui_mock"))
sys.path.insert(0, str(HERE))

from docx_text import docx_text, find_leaks  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import find_repo, find_fixtures, find_model  # noqa: E402


DEFAULT_FILTER = HERE.parent / "references" / "openwebui-filter.py"

# Группы категорий — как в upstream (src/core/categories.js, CATEGORY_GROUPS).
# ALWAYS = pii + ids: эти маскируются ВСЕГДА, и только их утечка считается
# провалом (upstream: scripts/eval-genres.mjs, ALWAYS + secret).
# commercial (COMPANY/CONTRACT/AMOUNT) — по политике 0.4 маскируются по желанию,
# поэтому их утечка — отдельный отчёт, а не FAIL.
ALWAYS_CATEGORIES = {
    "PASSPORT", "SNILS", "CARD", "EMAIL", "PHONE", "BIRTHDATE", "PERSON", "ADDRESS",
    "INN", "KPP", "OGRN", "ACCOUNT", "IBAN", "SWIFT", "BIK", "TAXID", "SSN", "URL",
}
COMMERCIAL_CATEGORIES = {"COMPANY", "CONTRACT", "AMOUNT"}


def load_filter_module(path: Path):
    spec = importlib.util.spec_from_file_location("owui_filter", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def gold_values(gold: dict):
    """Значения gold: [(category, value)] — с категорией, чтобы различать
    обязательные (pii/ids) и коммерческие."""
    out = []
    seen = set()
    for e in gold.get("entities", []):
        v = (e.get("value") or "").strip()
        cat = e.get("category") or ""
        if len(v) >= 4 and (cat, v) not in seen:
            seen.add((cat, v))
            out.append((cat, v))
    return out


def collect_cases(fx_root: Path, limit: int, with_pdf: bool = False, with_genres: bool = False):
    """Кейсы: документы с gold-разметкой.

    По умолчанию — heldout-корпус (12 DOCX, набор ревью upstream).
    С --with-pdf   — PDF жанрового корпуса (другой путь в ядре: pdf.js -> текстовая модель).
    С --with-genres — DOCX жанрового корпуса (8 жанров: чат, презентация, счёт, письмо,
                      инструкция, предложение, сверка, резюме).
    Порядок: heldout -> genres/docx -> genres/pdf.
    """
    cases = []

    def add(gold_path: Path, doc: Path, prefix: str):
        gold = json.loads(gold_path.read_text(encoding="utf-8"))
        values = gold_values(gold)
        if not values:
            return
        cases.append((prefix + gold_path.parent.name + "/" + gold_path.stem, doc, gold_path, values))

    for gold_path in sorted((fx_root / "heldout").glob("*/*.gold.json")):
        docx = gold_path.with_suffix("").with_suffix(".docx")
        if docx.is_file():
            add(gold_path, docx, "")

    if with_genres:
        for gold_path in sorted((fx_root / "genres").glob("*/*.gold.json")):
            docx = gold_path.with_suffix("").with_suffix(".docx")
            if docx.is_file():
                add(gold_path, docx, "docx/")

    if with_pdf:
        for gold_path in sorted((fx_root / "genres").glob("*/*.gold.json")):
            pdf = gold_path.with_suffix("").with_suffix(".pdf")
            if pdf.is_file():
                add(gold_path, pdf, "pdf/")

    return cases[:limit]


def run_cli(script: Path, repo: str, args: list):
    cmd = ["node", str(script), "--repo", repo, "--json"] + args
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "ошибка CLI").strip()[:300])
    return json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else {}



def significant_tokens(text: str):
    """Значимые токены значения: буквы/цифры, длина >= 3 (инициалы «Р.И.» отбрасываем)."""
    toks = [t.lower().replace("ё", "е") for t in re.findall(r"[0-9A-Za-zА-Яа-яЁё]+", text or "")]
    return [t for t in toks if len(t) >= 3]


def token_set(text: str):
    return set(significant_tokens(text))


def token_root_present(token: str, pool) -> bool:
    """Токен найден в pool с учётом падежного склонения.

    Сравниваем по общему началу: «сафину»/«сафина» (общее «сафин», 5 симв.),
    «маратовну»/«маратовна» (общее «маратовн», 8 симв.). Порог — общий префикс
    не короче max(4, длина_короче-1): это отсекает случайные совпадения
    («петя»/«пету» — общее «пет», 3 симв., порог 4 — не считается).
    """
    for t in pool:
        if t == token:
            return True
        n = 0
        for a, b in zip(token, t):
            if a != b:
                break
            n += 1
        need = max(4, min(len(token), len(t)) - 1)
        if n >= need:
            return True
    return False


def restored_text_of(path) -> str:
    """Текст восстановленного файла (docx — видимый текст, иначе как есть)."""
    if path.read_bytes()[:4] == b"PK\x03\x04":
        return docx_text(path)
    return path.read_text(encoding="utf-8", errors="replace")



def start_anon_service(work: Path, args):
    """Поднимает локальный HTTP-сервис обезличивания (references/anon_service.mjs).

    Возвращает (port, proc) или (None, None).
    """
    import socket
    import subprocess
    import time

    svc_src = HERE.parent / "references" / "anon_service.mjs"
    if not svc_src.is_file():
        return None, None

    # свободный порт
    with socket.socket() as s_:
        s_.bind(("127.0.0.1", 0))
        port = s_.getsockname()[1]

    env = dict(os.environ)
    env.update({
        "DOCX_ANON_REPO": args.repo,
        # HERE уже указывает на scripts/, поэтому путь без лишнего сегмента.
        "DOCX_ANON_WRAPPER": str(HERE / "anonymize.mjs"),
        "DOCX_ANON_WORKDIR": str(work / "svc-maps"),
        "DOCX_ANON_PORT": str(port),
        "DOCX_ANON_HOST": "127.0.0.1",
    })
    if args.ner and not args.no_ner:
        env["DOCX_ANON_NER"] = args.ner
    else:
        env.pop("DOCX_ANON_NER", None)

    proc = subprocess.Popen(
        ["node", str(svc_src)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    import urllib.request
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

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=find_repo())
    ap.add_argument("--fixtures", default=find_fixtures())
    ap.add_argument("--script", default=str(HERE / "anonymize.mjs"))
    ap.add_argument("--filter", default=str(DEFAULT_FILTER))
    ap.add_argument("--ner", default=find_model(), help="каталог модели NER (пусто — без модели)")
    ap.add_argument("--no-ner", action="store_true", help="явно без модели")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--with-pdf", action="store_true", help="добавить PDF из жанрового корпуса")
    ap.add_argument("--with-genres", action="store_true", help="добавить DOCX из жанрового корпуса")
    ap.add_argument("--offset", type=int, default=0, help="пропустить первые N кейсов (окно прогона)")
    args = ap.parse_args()

    all_cases = collect_cases(
        Path(args.fixtures), 10**6, with_pdf=args.with_pdf, with_genres=args.with_genres
    )
    cases = all_cases[args.offset : args.offset + args.limit]
    if not cases:
        print("НЕТ ФИКСТУР (offset=%d, лимит=%d)" % (args.offset, args.limit))
        return 2

    work = Path(tempfile.mkdtemp(prefix="docx-anon-test-"))
    mock_files = work / "mockfiles"
    mock_files.mkdir(parents=True)
    os.environ["MOCK_FILES_ROOT"] = str(mock_files)

    mod = load_filter_module(Path(args.filter))
    flt = mod.Filter()
    # Фильтр ходит в HTTP-сервис обезличивания: поднимаем локальный.
    svc_port, svc = start_anon_service(work, args)
    if svc is None:
        print("НЕ УДАЛОСЬ ПОДНЯТЬ СЕРВИС ОБЕЗЛИЧИВАНИЯ")
        return 3
    # Бэкенд Open WebUI пересоздаёт self.valves перед каждым вызовом
    # (utils/plugin.py:64-70) из значений в БД, а их там нет — значит
    # остаются дефолты класса. Поэтому переопределяем их так, чтобы они
    # применялись при каждом создании Valves().
    # В pydantic v2 присваивание model_fields[...].default НЕ действует —
    # нужен model_config['json_schema_extra'] или свойство. Поэтому заменяем
    # сам класс на подкласс с нужными дефолтами.
    class _TestValves(mod.Filter.Valves):
        SERVICE_URL: str = f"http://127.0.0.1:{svc_port}"
        WORKDIR: str = str(work / "maps")

    mod.Filter.Valves = _TestValves
    flt.valves = _TestValves()

    results = []
    for idx, (name, docx, gold_path, values) in enumerate(cases, 1):
        row = {"n": idx, "case": name, "fails": []}

        def fail(what):
            row["fails"].append(what)

        # ---------- A. CLI ----------
        stem = docx.stem
        anon = work / (stem + ".anon")
        mapping = work / (stem + ".map.json")
        restored = work / (stem + ".restored.docx")
        cli = ["--in", str(docx), "--out", str(anon), "--map", str(mapping), "--commercial"]
        # NER живёт в сервисе (DOCX_ANON_NER), а не в Valves фильтра:
        # CLI-часть теста запускает anonymize.mjs напрямую.
        ner_dir = "" if args.no_ner else args.ner
        if ner_dir and Path(ner_dir).is_dir():
            cli += ["--ner", ner_dir]
        try:
            stats = run_cli(Path(args.script), args.repo, cli)
            row["groups"] = stats.get("groups", 0)
            row["replacements"] = stats.get("replacements", 0)
        except Exception as e:  # noqa: BLE001
            fail(f"A1 обезличивание: {e}")
            results.append(row)
            continue

        # Утечки считаем по ОБЯЗАТЕЛЬНЫМ категориям (pii + ids) — так же, как
        # upstream (eval-genres.mjs: ALWAYS = pii + ids, secret = expected по этим
        # категориям). Коммерческие категории (COMPANY/CONTRACT/AMOUNT) по
        # политике 0.4 маскируются по желанию, поэтому их утечка — отдельный
        # отчёт (commercial_leaks), а не FAIL.
        must_values = [v for c, v in values if c in ALWAYS_CATEGORIES]
        comm_values = [v for c, v in values if c in COMMERCIAL_CATEGORIES]
        leaks = find_leaks(anon, must_values)
        row["leaks"] = leaks
        if leaks:
            fail(f"A2 утечки: {len(leaks)} ({leaks[0]})")
        row["commercial_leaks"] = find_leaks(anon, comm_values)

        try:
            rst = run_cli(Path(args.script), args.repo, ["--restore", str(anon), "--map-in", str(mapping), "--out", str(restored)])
            unresolved = rst.get("unresolved", 0)
            row["unresolved"] = unresolved
            if unresolved != 0:
                fail(f"A3 unresolved={unresolved}")
        except Exception as e:  # noqa: BLE001
            fail(f"A3 восстановление: {e}")
            results.append(row)
            continue

        is_zip = restored.read_bytes()[:4] == b"PK\x03\x04"
        if is_zip:
            restored_text = docx_text(restored)
        else:
            restored_text = restored.read_text(encoding="utf-8", errors="replace")
        # A4: проход восстановления отработал.
        # Сравнивать строки нельзя: ядро СКЛОНЯЕТ ФИО по контексту
        # ("Сафину Гульнару Маратовну" -> "Сафина Гульнара Маратовна"), а в карте
        # лежит один канон на сущность. Поэтому сверяем по КОРНЮ ТОКЕНОВ:
        # каждый значимый токен значения должен найтись в восстановленном тексте
        # с общим началом (допускаем падежное склонение).
        # Плюс два жёстких инварианта: незакрытых меток нет, unresolved == 0.
        map_entries = json.loads(mapping.read_text(encoding="utf-8")).get("entries", [])
        restored_tokens = token_set(restored_text_of(restored))
        missing = []
        for e in map_entries:
            forms = [f for f in [e.get("value"), e.get("original")] + list(e.get("variants") or []) if f]
            if not forms:
                continue
            # любая форма сущности подходит: канон или вариант
            ok = False
            for form in forms:
                toks = significant_tokens(form)
                if not toks:
                    ok = True
                    break
                if all(token_root_present(t, restored_tokens) for t in toks):
                    ok = True
                    break
            if not ok:
                missing.append(e.get("token"))
        row["not_restored"] = missing
        if missing:
            fail(f"A4 сущности не вернулись: {missing[:5]}")

        left = re.findall(r"\[[А-ЯЁA-Z]+_\d+\]", restored_text)
        row["placeholders_left"] = left
        if left:
            fail(f"A4 остались метки: {sorted(set(left))[:5]}")
        # ---------- B. Open WebUI-фильтр ----------
        fid = "f:" + docx.name
        shutil.copyfile(docx, mock_files / docx.name)
        req = {
            "model": "test-model",
            "messages": [
                {"role": "system", "content": "Ты помощник."},
                # Блок <attached_files> backend добавляет ДО inlet
                # (middleware.py:2369 → add_file_context): id файла, по которому
                # модель может вызвать view_file и прочитать оригинал.
                {"role": "user", "content":
                    "<attached_files>\n"
                    f'<file type="file" id="{fid}" url="{fid}" '
                    f'content_type="application/octet-stream" name="{docx.name}"/>\n'
                    "</attached_files>\n\n"
                    "Разбери документ и найди подвох."},
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
            fail(f"B1 inlet: {e}")
            results.append(row)
            continue

        text = out["messages"][-1].get("content", "")
        if isinstance(text, list):
            text = " ".join(p.get("text", "") for p in text if isinstance(p, dict))
        row["chars_to_model"] = len(text)

        garbage = ("PK\x03\x04" in text) or (text.count("\ufffd") > 3) or any(ord(c) < 9 for c in text[:2000])
        if garbage:
            fail("B2 модель получила бинарный мусор")
        if len(text.strip()) < 100:
            fail(f"B2 слишком мало текста: {len(text.strip())}")

        blob = json.dumps(out, ensure_ascii=False, default=str)
        body_leaks = [v for v in must_values if v in blob]
        row["body_leaks"] = body_leaks
        if body_leaks:
            fail(f"B3 утечки в теле запроса: {body_leaks[:3]}")
        row["commercial_body_leaks"] = [v for v in comm_values if v in blob]

        files_left = bool(out.get("files")) or bool(out.get("metadata", {}).get("files"))
        msg_files = any(m.get("files") for m in out.get("messages", []))
        if files_left or msg_files:
            fail("B4 files не вычищены")

        # B4b: блок <attached_files> с id файла добавляет backend ДО inlet
        # (middleware.py:2369). По этому id модель может вызвать view_file и
        # прочитать оригинал — значит блок должен быть убран.
        if "<attached_files>" in blob:
            fail("B4b блок <attached_files> остался в теле запроса")

        # B4c: оригинал не должен быть доступен по id файла. Фильтр затирает
        # files.data['content'] обезличенным текстом — проверяем, что там
        # действительно нет исходных значений.
        from open_webui.models.files import Files as _MockFiles
        scrubbed = asyncio.run(_MockFiles.get_file_by_id(fid))
        scrub_txt = (getattr(scrubbed, "data", None) or {}).get("content", "") or ""
        scrub_leaks = [v for v in must_values if v in scrub_txt]
        row["scrubbed_leaks"] = scrub_leaks
        if scrub_leaks:
            fail(f"B4c по id файла доступны исходные ПДн: {scrub_leaks[:3]}")
        elif not scrub_txt.strip():
            fail("B4c запись файла не заменена обезличенным текстом")

        mp = Path(flt.valves.WORKDIR) / (flt._session_key(req) + ".json")
        if mp.exists():
            saved = json.loads(mp.read_text(encoding="utf-8"))
            maps = saved.get("files") or {}
            entries = []
            for m in maps.values():
                entries.extend((m or {}).get("entries", []))
            if entries:
                token = entries[0].get("placeholder") or entries[0].get("token")
                ans = {"id": "a1", "role": "assistant", "content": f"Проверка: {token}."}
                res = asyncio.run(flt.outlet({"messages": [ans], "chat_id": req["metadata"]["chat_id"]}))
                if token in res["messages"][0]["content"]:
                    fail("B5 outlet не восстановил метку")
            else:
                fail("B5 карта замен пуста")
        else:
            fail("B5 нет карты замен для outlet")

        results.append(row)

    # ---------- отчёт ----------
    ok = sum(1 for r in results if not r["fails"])
    mode = "с NER" if (args.ner and not args.no_ner) else "без NER"
    print("=" * 92)
    print(f"ТЕСТ docx-anonymizer: {ok}/{len(results)} кейсов прошли (режим: {mode})")
    print("=" * 92)
    for r in results:
        mark = "OK  " if not r["fails"] else "FAIL"
        print(f"[{mark}] {r['n']:2d}. {r['case']}")
        if not r["fails"]:
            print(
                f"        групп={r.get('groups')} замен={r.get('replacements')} "
                f"текст_модели={r.get('chars_to_model')} симв. утечек=0 unresolved={r.get('unresolved')}"
                + (
                    f" (коммерческие не замаскированы: {len(r.get('commercial_leaks') or [])})"
                    if r.get("commercial_leaks")
                    else ""
                )
            )
        for f in r["fails"]:
            print(f"        ✗ {f}")
    print("-" * 92)
    print(f"ИТОГ: {ok}/{len(results)}")
    print(f"рабочие каталоги: {work}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
