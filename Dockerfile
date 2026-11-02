# Образ сервиса обезличивания документов (Open WebUI DOCX Anonymizer).
#
#   docker build -t ghcr.io/zhenyarus39/owui-docx-anonymizer-service:0.1.0 .
#
# Что внутри:
#   /srv/anon/anon_service.mjs      HTTP-сервис обезличивания
#   /srv/anon/office_extract.mjs    разбор xlsx/pptx/odt/doc
#   /srv/anon/scripts/anonymize.mjs CLI-обёртка ядра
#
# Что НЕ внутри (монтируется при запуске, read-only):
#   /srv/repo  — ядро upstream argmaster-dev/docx-anonymizer (v0.4.0)
#   /srv/ner   — модель распознавания ПДн (ONNX, ~30 МБ)
#
# Так ядро и модель обновляются без пересборки образа.
FROM node:22-alpine

# LibreOffice Writer — разбор бинарного .doc (Word 97–2003). Без него
# этот формат не читает никто, и такие документы блокируются.
RUN apk add --no-cache libreoffice-writer \
    && rm -rf /var/cache/apk/*

COPY references/anon_service.mjs   /srv/anon/anon_service.mjs
COPY references/office_extract.mjs /srv/anon/office_extract.mjs
COPY scripts/anonymize.mjs         /srv/anon/scripts/anonymize.mjs

# health-check: сервис должен отвечать на /health
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8971/health >/dev/null 2>&1 || exit 1

# Сервис слушает только localhost (DOCX_ANON_HOST=127.0.0.1). Наружу он
# доступен с того же хоста, где стоит Open WebUI (--network host).
EXPOSE 8971

WORKDIR /srv/anon
CMD ["node", "/srv/anon/anon_service.mjs"]
