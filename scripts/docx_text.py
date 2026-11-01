# -*- coding: utf-8 -*-
"""Читаемый текст docx и поиск утечек в пакете.

Два уровня, оба нужны:

1. `docx_text(path)` — видимый текст (<w:t> из всех частей). Используется, чтобы
   отдать модели читаемый текст вместо бинарного мусора.
   Внимание: в сыром XML теги с ПРЕФИКСОМ имени (w:t, w:delText), а не с полным
   пространством имён. Регулярка по {namespace}t находит НОЛЬ — тихая ложь.

2. `find_leaks(path, values)` — поиск значений во ВСЕХ XML-частях пакета тремя
   способами, как в upstream (tests/integration/support/pipeline.js, findLeaks):
     - сырой XML (значение может быть разбито тегами: "Иванов<b>ов</b>");
     - XML с раскрытыми сущностями;
     - XML без тегов (текст целиком).
   Это строже, чем поиск только по <w:t>: утечка в атрибуте, в комментарии,
   в имени файла внутри .rels или в author="..." тоже находится.

Зависимости: только стандартная библиотека.
"""
import re
import zipfile

# <w:t ...>текст</w:t> и <w:delText ...>текст</w:delText> (удалённый текст рецензий)
TEXT_RE = re.compile(r"<(?:[A-Za-z0-9]+:)?(?:t|delText)(?:\s[^>]*)?>(.*?)</(?:[A-Za-z0-9]+:)?(?:t|delText)>", re.S)

_STRIP_TAGS = re.compile(r"<[^>]+>")


def unescape_xml(s: str) -> str:
    """Раскрыть XML-сущности (как unesc в upstream)."""
    s = (
        s.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
        .replace("&amp;", "&")
    )
    return re.sub(
        r"&(#x[0-9a-fA-F]+|#[0-9]+);",
        lambda m: chr(int(m.group(1)[2:], 16)) if m.group(1).startswith("#x") else chr(int(m.group(1)[1:])),
        s,
    )


def _is_zip(path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"PK\x03\x04"
    except OSError:
        return False


def docx_text(path) -> str:
    """Видимый текст docx: содержимое всех <w:t> из всех частей пакета."""
    chunks = []
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if not name.endswith(".xml"):
                continue
            raw = z.read(name).decode("utf-8", "replace")
            texts = [t for t in TEXT_RE.findall(raw) if t.strip()]
            if texts:
                chunks.append("\n".join(texts))
    return "\n".join(chunks)


def package_xml_parts(path) -> dict:
    """Все XML-части пакета: имя -> текст."""
    parts = {}
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if name.endswith((".xml", ".rels")):
                parts[name] = z.read(name).decode("utf-8", "replace")
    return parts


def find_leaks(path, values, min_length: int = 4):
    """Значения, оставшиеся в пакете.

    Метод как в upstream: для каждой XML-части ищем значение в трёх
    представлениях — сырой XML, XML с раскрытыми сущностями, XML без тегов.
    Регистр не учитывается.

    @param path: путь к docx (zip) или к текстовому файлу
    @param values: искомые значения (строки)
    @param min_length: короче — пропускаем (ложные срабатывания на коротких словах)
    @returns list[str]: "значение @ часть"
    """
    if not _is_zip(path):
        text = open(path, encoding="utf-8", errors="replace").read().lower()
        return [v for v in values if len(v) >= min_length and v.lower() in text]

    leaks = []
    for name, xml in package_xml_parts(path).items():
        hay = [
            xml.lower(),
            unescape_xml(xml).lower(),
            unescape_xml(_STRIP_TAGS.sub("", xml)).lower(),
        ]
        for v in values:
            if len(v) < min_length:
                continue
            if any(v.lower() in h for h in hay):
                leaks.append(f"{v} @ {name}")
    return leaks
