# -*- coding: utf-8 -*-
"""Тестовые фикстуры с ПДн для живой проверки — без внешних зависимостей.

DOCX собирается как zip руками (три XML-части) — этого достаточно, чтобы
ядро распознало формат и нашло сущности.

PDF нужен настоящий: стандартные шрифты PDF (WinAnsi) не умеют кириллицу,
поэтому «рукописный» PDF извлекается как `????` и ядро считает его сканом
(PDF_NO_TEXT). Поэтому PDF-фикстура берётся из репозитория upstream —
он сгенерирован через Chromium со встроенными шрифтами.
"""
import os
import zipfile

DOCX_TEXT = (
    "Договор N 42/26\n"
    "Гражданин РФ Белоусов Тимофей Аркадьевич, паспорт 36 15 482913, "
    "выдан 12.05.2015, зарегистрирован по адресу: "
    "г. Самара, ул. Молодогвардейская, д. 10, кв. 5.\n"
    "Исполнитель: Гусейнова Лейла Руслановна.\n"
)

# Проба ПДн в PDF-фикстуре (из upstream tests/fixtures/pdf/ip-contract.pdf).
PDF_PROBES = ["Гусев", "Белоусов"]


def make_docx(path="/tmp/live_check.docx"):
    """Минимальный валидный DOCX с ПДн."""
    ct = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '</Relationships>'
    )
    body = "".join(
        '<w:p><w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p>' % line
        for line in DOCX_TEXT.split("\n")
    )
    doc = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>" + body + "</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)
    return path


def make_pdf(path="/tmp/live_check.pdf"):
    """PDF-фикстура: копия рабочей фикстуры upstream.

    Кириллица в PDF требует встроенного шрифта с Unicode — «рукописный» PDF
    извлекается как `????` и ядро считает его сканом. Upstream генерирует
    фикстуры через Chromium, поэтому берём готовую.
    """
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "live_check.pdf")
    if not os.path.isfile(src):
        raise FileNotFoundError(
            "PDF-фикстура не найдена: %s (возьми из upstream tests/fixtures/pdf/ip-contract.pdf)" % src
        )
    with open(src, "rb") as f:
        data = f.read()
    # Всегда перезаписываем: в /tmp мог остаться битый файл от прошлого прогона
    # (например, «рукописный» PDF без юникод-шрифта — ядро считает его сканом).
    with open(path, "wb") as f:
        f.write(data)
    return path
