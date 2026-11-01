#!/usr/bin/env python3
"""Edge-случаи: что должно корректно ОТКАЗЫВАТЬСЯ, а не падать.

Кейсы:
  E1 PDF с паролем      → отказ PDF_ENCRYPTED, без падения
  E2 пустой PDF         → отказ PDF_EMPTY
  E3 PDF-скан (без текста) → отказ PDF_NO_TEXT
  E4 не-docx (txt в .docx) → отказ NOT_DOCX
  E5 .xlsx / .pptx      → отказ «неподдерживаемый формат»
  E6 битый zip           → отказ NOT_DOCX, без падения
  E7 файл с кириллицей и пробелами в имени → обезличивается, карта на месте
  E8 каталога/нет файла → понятная ошибка, не трассировка

Каждый кейс проверяет код выхода и наличие читаемого сообщения.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
FIX = Path("/tmp/docx-anonymizer/tests/fixtures")

results = []


def run_cli(args, script=None):
    script = script or str(SKILL / "scripts" / "anonymize.mjs")
    cmd = ["node", script, "--repo", "/tmp/docx-anonymizer"] + args
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def case(name, expect_code, args, must_contain=None, setup=None):
    tmp = Path(tempfile.mkdtemp())
    if setup:
        setup(tmp)
    rc, out = run_cli([a.replace("{TMP}", str(tmp)) for a in args])
    ok = rc == expect_code
    detail = out.strip().splitlines()[-1][:120] if out.strip() else "(пусто)"
    if must_contain and must_contain.lower() not in out.lower():
        ok = False
        detail += f"  [нет '{must_contain}']"
    results.append((name, ok, rc, detail))


# --- E1-E6: отказы ---
case(
    "E1 PDF с паролем",
    1,
    ["--in", str(FIX / "pdf" / "encrypted.pdf"), "--out", "{TMP}/o.md", "--map", "{TMP}/m.json"],
    "ENCRYPTED",
)
case(
    "E2 пустой PDF",
    1,
    ["--in", str(FIX / "pdf" / "empty.pdf"), "--out", "{TMP}/o.md", "--map", "{TMP}/m.json"],
    "PDF_EMPTY",
)
case(
    "E3 PDF-скан (нет текстового слоя)",
    1,
    ["--in", str(FIX / "pdf" / "scan.pdf"), "--out", "{TMP}/o.md", "--map", "{TMP}/m.json"],
    "PDF_NO_TEXT",
)
case(
    "E4 не-docx (текст с расширением .docx)",
    2,
    ["--in", "{TMP}/fake.docx", "--out", "{TMP}/o.docx", "--map", "{TMP}/m.json"],
    "не является ZIP-архивом",
    setup=lambda t: (t / "fake.docx").write_text("это не docx, а текст\n", encoding="utf-8"),
)
case(
    "E5 .xlsx не поддерживается",
    2,
    ["--in", "{TMP}/book.xlsx", "--out", "{TMP}/o.xlsx", "--map", "{TMP}/m.json"],
    "неподдерживаемый формат",
    setup=lambda t: (t / "book.xlsx").write_bytes(b"PK\x03\x04" + b"\x00" * 100),
)
case(
    "E6 битый zip",
    2,
    ["--in", "{TMP}/broken.docx", "--out", "{TMP}/o.docx", "--map", "{TMP}/m.json"],
    "это ZIP, но не DOCX",
    setup=lambda t: (t / "broken.docx").write_bytes(b"PK\x03\x04" + b"\x00" * 50),
)
case(
    "E8 файла нет",
    2,
    ["--in", "{TMP}/missing.docx", "--out", "{TMP}/o.docx", "--map", "{TMP}/m.json"],
    "не найден",
)

# --- E7: кириллица и пробелы в имени ---
def setup_cyr(t):
    src = FIX / "heldout" / "memo" / "04-sluzhebnaya.docx"
    (t / "Служебная записка (черновик) — финал.docx").write_bytes(src.read_bytes())


tmp7 = Path(tempfile.mkdtemp())
setup_cyr(tmp7)
src7 = tmp7 / "Служебная записка (черновик) — финал.docx"
rc, out = run_cli(
    ["--in", str(src7), "--out", str(tmp7 / "out.docx"), "--map", str(tmp7 / "map.json"), "--json"]
)
map_ok = (tmp7 / "map.json").is_file() and (tmp7 / "map.json").stat().st_size > 100
out_ok = (tmp7 / "out.docx").is_file() and (tmp7 / "out.docx").stat().st_size > 100
results.append(
    (
        "E7 кириллица и пробелы в имени",
        rc == 0 and map_ok and out_ok,
        rc,
        f"map={'да' if map_ok else 'нет'} out={'да' if out_ok else 'нет'}",
    )
)

print("=" * 84)
print("EDGE-СЛУЧАИ: корректный отказ вместо падения")
print("=" * 84)
ok = sum(1 for r in results if r[1])
for name, good, rc, detail in results:
    print("[%s] %-38s exit=%-3s %s" % ("OK  " if good else "FAIL", name, rc, detail))
print("-" * 84)
print("ИТОГ: %d/%d" % (ok, len(results)))
print("=" * 84)
sys.exit(0 if ok == len(results) else 1)
