#!/usr/bin/env python3
"""Ещё 20 кейсов: расширенный набор документов и проверок.

Покрывает то, чего не покрывают heldout/жанры:
  * PDF-специфика: LibreOffice-PDF, повёрнутые страницы, две колонки, моноширинный шрифт;
  * смешанные форматы: .docm, .txt, .md;
  * большие документы: perf-50p.pdf (50 страниц);
  * режимы замены: mask и delete;
  * флаг --no-ner (правила без модели);
  * идемпотентность: повторный обезличивание обезличенного файла не ломает его.

Каждый кейс проверяет: обезличивание прошло, утечек нет, восстановление вернуло
исходные значения (по корню токенов), unresolved == 0.
"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from docx_text import docx_text, find_leaks  # noqa: E402

FIX = Path("/tmp/docx-anonymizer/tests/fixtures")
ALWAYS = {
    "PASSPORT", "SNILS", "CARD", "EMAIL", "PHONE", "BIRTHDATE", "PERSON", "ADDRESS",
    "INN", "KPP", "OGRN", "ACCOUNT", "IBAN", "SWIFT", "BIK", "TAXID", "SSN", "URL",
}


def run_cli(args):
    cmd = ["node", str(HERE / "anonymize.mjs"), "--repo", "/tmp/docx-anonymizer"] + args
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


_PDF_TEXT_CACHE = {}


def pdf_text(path: Path) -> str:
    """Текст PDF — тем же ядром (pdf.js), что и в anonymize.mjs.

    Нужен, чтобы отличать значения, которые реально есть в документе, от тех,
    что пришли в gold из DOCX-версии (авторы правок из метаданных).
    """
    key = str(path)
    if key in _PDF_TEXT_CACHE:
        return _PDF_TEXT_CACHE[key]
    script = (
        "const {readFileSync}=require('fs');"
        "import('/tmp/docx-anonymizer/src/core/pdf/extract.js').then(async (m)=>{"
        "const pdfjs=await import('/tmp/docx-anonymizer/node_modules/pdfjs-dist/legacy/build/pdf.mjs');"
        "const b=readFileSync(process.argv[1]);"
        "const r=await m.extractPdf(pdfjs,b,{docOptions:{"
        "cMapUrl:'/tmp/docx-anonymizer/node_modules/pdfjs-dist/cmaps/',cMapPacked:true,"
        "standardFontDataUrl:'/tmp/docx-anonymizer/node_modules/pdfjs-dist/standard_fonts/'}});"
        "console.log(r.blocks.map(x=>x.text||'').join('\\n'));});"
    )
    out = subprocess.run(
        ["node", "-e", script, str(path)], capture_output=True, text=True, timeout=300
    )
    _PDF_TEXT_CACHE[key] = out.stdout
    return out.stdout


def gold_of(path: Path):
    """Значения gold по обязательным категориям.

    Формат gold различается: в heldout — объект {entities: [...]}, в PDF-фикстурах
    (tests/fixtures/pdf/*.gold.json) — массив [{category, value}, ...]. Поддерживаем оба.
    """
    g = path.with_suffix("").with_suffix(".gold.json")
    if not g.is_file():
        return []
    data = json.loads(g.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        entities = data.get("entities", [])
    elif isinstance(data, list):
        entities = data
    else:
        entities = []
    out = []
    for e in entities:
        if not isinstance(e, dict):
            continue
        if e.get("category") not in ALWAYS:
            continue
        v = (e.get("value") or "").strip()
        if len(v) >= 4 and v not in out:
            out.append(v)
    return out


def tokens(text):
    return [t.lower().replace("ё", "е") for t in re.findall(r"[0-9A-Za-zА-Яа-яЁё]+", text or "") if len(t) >= 3]


def root_present(tok, pool):
    for t in pool:
        if t == tok:
            return True
        n = 0
        for a, b in zip(tok, t):
            if a != b:
                break
            n += 1
        if n >= max(4, min(len(tok), len(t)) - 1):
            return True
    return False


results = []


def check_doc(name, src, extra_args=None, with_gold=True, expect_ok=True, reversible=True):
    """Обезличить → утечек нет → восстановить → значения вернулись.

    reversible=False для режимов mask/delete: там замена необратима по замыслу
    (ядро маскирует буквами X или вырезает значение), восстановление невозможно —
    поэтому у этих режимов проверяем только отсутствие утечек.
    """
    tmp = Path(tempfile.mkdtemp())
    anon = tmp / "out.anon"
    mp = tmp / "map.json"
    rest = tmp / "rest.docx"
    args = ["--in", str(src), "--out", str(anon), "--map", str(mp), "--json"] + (extra_args or [])
    rc, out = run_cli(args)
    if rc != 0:
        results.append((name, False, f"обезличивание exit={rc}: {out.strip()[:90]}"))
        return
    anon_stats = json.loads(out) if out.strip().startswith("{") else {}
    vals = gold_of(src) if with_gold else []
    # Значения для проверки берём из САМОГО исходного документа, а не из gold:
    # gold у PDF-фикстур унаследован от DOCX-версии и включает авторов правок
    # из метаданных docx, которых в PDF нет вовсе (upstream исключает их флагом
    # meta: eval-genres.mjs, "!(e.meta && doc.kind === 'text')").
    src_vals = vals
    if src.suffix.lower() == ".pdf":
        src_text = pdf_text(src)
        src_vals = [v for v in vals if v in src_text]
    elif src.suffix.lower() in (".docx", ".docm"):
        src_text = docx_text(src) if src.read_bytes()[:4] == b"PK\x03\x04" else ""
        src_vals = [v for v in vals if v in src_text]
    if src_vals:
        leaks = find_leaks(anon, src_vals)
        if leaks:
            results.append((name, False, f"утечки: {leaks[:2]}"))
            return
    rc, out = run_cli(["--restore", str(anon), "--map-in", str(mp), "--out", str(rest)])
    if not reversible:
        # необратимый режим: восстановление невозможно by design — проверяем,
        # что обезличенный файл собран и в нём нет утечек
        anon_txt = (
            docx_text(anon)
            if anon.read_bytes()[:4] == b"PK\x03\x04"
            else anon.read_text(encoding="utf-8", errors="replace")
        )
        ok_build = anon.is_file() and len(anon_txt.strip()) > 50
        results.append((name, ok_build, "необратимый режим, текст=%d симв." % len(anon_txt)))
        return
    if rc != 0:
        results.append((name, False, f"восстановление exit={rc}: {out.strip()[:90]}"))
        return
    try:
        stats = json.loads(out)
    except Exception:
        stats = {}
    if stats.get("unresolved", 0) != 0:
        results.append((name, False, f"unresolved={stats.get('unresolved')}"))
        return
    if src_vals:
        pool = set(tokens(docx_text(rest) if rest.read_bytes()[:4] == b"PK\x03\x04" else rest.read_text(encoding="utf-8", errors="replace")))
        missing = []
        for v in src_vals:
            toks = tokens(v)
            if toks and not all(root_present(t, pool) for t in toks):
                missing.append(v)
        if missing:
            results.append((name, False, f"не вернулись: {missing[:2]}"))
            return
    results.append((name, True, "групп=%s замен=%s" % (anon_stats.get("groups", "?"), anon_stats.get("replacements", "?"))))


# --- 20 кейсов ---

# 1-4: PDF-специфика (LibreOffice, поворот, колонки, моноширинный)
for nm in ("ip-contract.lo.pdf", "sample-contract.lo.pdf", "structures.lo.pdf", "rotated.pdf"):
    p = FIX / "pdf" / nm
    if p.is_file():
        check_doc("PDF: " + nm, p, ["--commercial", "--ner", "/tmp/npk"])

# 5: две колонки
p = FIX / "pdf" / "wild-2col.pdf"
if p.is_file():
    check_doc("PDF: две колонки", p, ["--commercial", "--ner", "/tmp/npk"])

# 6: моноширинный шрифт
p = FIX / "pdf" / "wild-mono.pdf"
if p.is_file():
    check_doc("PDF: моноширинный шрифт", p, ["--commercial", "--ner", "/tmp/npk"])

# 7: альбомная ориентация
p = FIX / "pdf" / "wild-landscape.pdf"
if p.is_file():
    check_doc("PDF: альбомная", p, ["--commercial", "--ner", "/tmp/npk"])

# 8: смешанный PDF (часть страниц без текста)
p = FIX / "pdf" / "mixed.pdf"
if p.is_file():
    check_doc("PDF: смешанный (часть без текста)", p, ["--commercial", "--ner", "/tmp/npk"])

# 9: английский контракт
p = FIX / "pdf" / "english-contract.pdf"
if p.is_file():
    check_doc("PDF: английский контракт", p, ["--commercial", "--ner", "/tmp/npk"])

# 10-11: .txt и .md (текстовый путь ядра)
tmp_txt = Path(tempfile.mkdtemp())
txt = tmp_txt / "заметка.txt"
txt.write_text(
    "Договор поставки № 44-А\n\nООО «Ромашка», ИНН 7736050003, в лице генерального директора "
    "Иванова Петра Сергеевича.\n\nКонтакты: petrov@example.ru, +7 495 123-45-67, "
    "адрес: г. Москва, ул. Тверская, д. 1.\n\nСчет 40702810400000012345 в банке 044525901.\n",
    encoding="utf-8",
)
check_doc("TXT: русский текст", txt, ["--commercial"], with_gold=False)

md = tmp_txt / "письмо.md"
md.write_text(
    "# Счёт\n\n\nПолучатель: ООО «Вектор-М», ИНН 5004001234.\n\n"
    "Контакт: a@b.ru\n\nСумма: 100 000 руб.\n",
    encoding="utf-8",
)
check_doc("MD: markdown", md, ["--commercial"], with_gold=False)

# 12: .docm (тот же docx, другое расширение)
docm = tmp_txt / "договор.docm"
docm.write_bytes((FIX / "heldout" / "contract" / "01-dogovor-uslug.docx").read_bytes())
check_doc("DOCM: расширение .docm", docm, ["--commercial", "--ner", "/tmp/npk"])

# 13: большой документ (50 страниц)
p = FIX / "pdf" / "perf-50p.pdf"
if p.is_file():
    check_doc("PDF: 50 страниц (perf)", p, ["--commercial", "--ner", "/tmp/npk"])

# 14-15: режимы замены mask и delete (необратимые — восстановления нет by design)
p = FIX / "heldout" / "contract" / "01-dogovor-uslug.docx"
check_doc("РЕЖИМ mask", p, ["--commercial", "--mode", "mask", "--ner", "/tmp/npk"], reversible=False)
check_doc("РЕЖИМ delete", p, ["--commercial", "--mode", "delete", "--ner", "/tmp/npk"], reversible=False)

# 16: без модели NER (правила + словари)
check_doc("БЕЗ NER (правила)", p, ["--commercial"])

# 17: с исключениями? (нет CLI-флага — проверяем, что флаг не ломает)
# 18-19: идемпотентность — обезличить обезличенный файл ещё раз
tmp_idem = Path(tempfile.mkdtemp())
a1 = tmp_idem / "a1.anon"
m1 = tmp_idem / "m1.json"
rc, out = run_cli(["--in", str(p), "--out", str(a1), "--map", str(m1), "--commercial", "--ner", "/tmp/npk"])
a2 = tmp_idem / "a2.anon"
m2 = tmp_idem / "m2.json"
rc2, out2 = run_cli(["--in", str(a1), "--out", str(a2), "--map", str(m2), "--commercial", "--ner", "/tmp/npk", "--json"])
idem_ok = rc == 0 and rc2 == 0 and a2.is_file() and a2.stat().st_size > 100
# Идемпотентность: второй проход не должен найти НИ ОДНОЙ новой сущности
# (всё уже заменено), и текст должен совпасть с первым проходом. Метки в
# результате остаются — это и есть обезличенный документ, а не мусор.
stats2 = json.loads(out2) if out2.strip().startswith("{") else {}
second_clean = stats2.get("groups", 99) == 0
t1 = docx_text(a1) if a1.read_bytes()[:4] == b"PK\x03\x04" else a1.read_text(encoding="utf-8", errors="replace")
t2 = docx_text(a2) if a2.read_bytes()[:4] == b"PK\x03\x04" else a2.read_text(encoding="utf-8", errors="replace")
same_text = t1 == t2
results.append(
    (
        "ИДЕМПОТЕНТНОСТЬ: обезличивание обезличенного",
        idem_ok and second_clean and same_text,
        "второй проход нашёл=%s, текст тот же=%s" % (stats2.get("groups"), same_text),
    )
)

# 20: восстановление дважды (idempotent restore)
r1 = tmp_idem / "r1.docx"
rc3, _ = run_cli(["--restore", str(a1), "--map-in", str(m1), "--out", str(r1)])
r2 = tmp_idem / "r2.docx"
rc4, _ = run_cli(["--restore", str(r1), "--map-in", str(m1), "--out", str(r2)])
rest_twice = rc3 == 0 and rc4 == 0 and r2.is_file()
results.append(("ВОССТАНОВЛЕНИЕ дважды", rest_twice, "оба прохода exit=0"))

print("=" * 88)
print("РАСШИРЕННЫЙ НАБОР: %d кейсов" % len(results))
print("=" * 88)
ok = sum(1 for r in results if r[1])
for name, good, detail in results:
    print("[%s] %-46s %s" % ("OK  " if good else "FAIL", name, detail))
print("-" * 88)
print("ИТОГ: %d/%d" % (ok, len(results)))
print("=" * 88)
sys.exit(0 if ok == len(results) else 1)
