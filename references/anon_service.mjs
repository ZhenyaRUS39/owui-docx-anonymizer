#!/usr/bin/env node
/**
 * HTTP-сервис обезличивания для Open WebUI (Open WebUI v0.12.0).
 *
 * Зачем сервис, а не node внутри контейнера open-webui:
 *   - в контейнере open-webui нет node вообще (проверено: node/npm отсутствуют);
 *   - рекомендуемый подход Open WebUI для внешних интеграций: «call your own
 *     local HTTP service over plain HTTP — that is the simplest and needs no
 *     Open WebUI credentials at all».
 *
 * Слушает в HOST-сети (как сам open-webui), чтобы адрес не менялся при
 * пересоздании контейнеров: Open WebUI работает в network_mode=host.
 *
 * Эндпоинты:
 *   GET  /health   → {"ok":true,"ner":true|false,...}
 *   POST /anonymize  multipart: file=<bytes> → {"text","map","stats","id"}
 *   POST /restore    JSON {"map":{...},"text":"..."} → {"text":"..."}
 *
 * Ничего не ходит наружу: ядро и модель NER лежат на диске.
 */
import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { readFile, writeFile, mkdir, rm } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { dirname, join, resolve, extname, basename } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { tmpdir } from 'node:os';
import { extractOfficeText } from './office_extract.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const SKILL_DIR = process.env.DOCX_ANON_SKILL_DIR || dirname(HERE);
const REPO = process.env.DOCX_ANON_REPO || '/srv/repo';

// Список госорганов и банков из ядра. Нужен для пост-фильтрации карты замен:
// модель NER маскирует их сама (минуя правило govEntities), а переключатель
// «Госорганы: не трогать» должен оставлять их как есть.
let GOV_SET = new Set();
try {
  const lex = await import(join(REPO, 'src/core/detect/names/lexicon.js'));
  GOV_SET = lex.GOV_ENTITIES instanceof Set ? lex.GOV_ENTITIES : new Set();
} catch {
  // ядро недоступно — фильтрация госорганов просто не работает
}
const NER_DIR = process.env.DOCX_ANON_NER || '/srv/ner';
const WRAPPER = process.env.DOCX_ANON_WRAPPER || join(SKILL_DIR, 'scripts', 'anonymize.mjs');
const WORKDIR = process.env.DOCX_ANON_WORKDIR || '/srv/state';
const PORT = Number(process.env.DOCX_ANON_PORT || 8971);
// По умолчанию — только localhost: сервис обрабатывает ПДн, и открывать его
// в сеть без авторизации нельзя. Открытый адрес — осознанное решение
// оператора (DOCX_ANON_HOST), но тогда обязателен DOCX_ANON_TOKEN.
const HOST = process.env.DOCX_ANON_HOST || '127.0.0.1';
// Токен доступа. Если задан — требуется в заголовке Authorization.
// Если не задан, а сервис слушает не только localhost — сервис НЕ СТАРТУЕТ.
const TOKEN = process.env.DOCX_ANON_TOKEN || '';
const COMMERCIAL = process.env.DOCX_ANON_COMMERCIAL !== 'false';
const MODE = process.env.DOCX_ANON_MODE || 'placeholder';
// Использовать модель NER по умолчанию (если модель установлена).
const NER_ENABLED = process.env.DOCX_ANON_NER_ENABLED !== 'false';
// Порог NER по умолчанию (якорь ядра — 0.7).
const NER_THRESHOLD = process.env.DOCX_ANON_NER_THRESHOLD
  ? Number(process.env.DOCX_ANON_NER_THRESHOLD)
  : undefined;
// Госорганы: true — не трогать, false — маскировать.
const GOV_ENTITIES = process.env.DOCX_ANON_GOV_ENTITIES === 'false' ? false : undefined;
// Словарный детектор ФИО третьих лиц.
const DICT_NAMES = process.env.DOCX_ANON_DICT_NAMES === 'false' ? false : undefined;
// Метаданные и встроенные объекты.
const KEEP_METADATA = process.env.DOCX_ANON_KEEP_METADATA === 'true' ? true : undefined;
const STRIP_EMBEDDED = process.env.DOCX_ANON_STRIP_EMBEDDED === 'true' ? true : undefined;
// Язык меток и язык распознавания.
const PLACEHOLDER_LANGUAGE = process.env.DOCX_ANON_PLACEHOLDER_LANGUAGE || undefined;
const LANGUAGE = process.env.DOCX_ANON_LANGUAGE || undefined;

if (HOST !== '127.0.0.1' && HOST !== 'localhost' && !TOKEN) {
  console.error(
    'ОТКАЗ ЗАПУСКА: сервис слушает ' + HOST + ' (не только localhost), ' +
    'но DOCX_ANON_TOKEN не задан. Сервис обрабатывает персональные данные — ' +
    'без токена его нельзя открывать в сеть. Задайте DOCX_ANON_TOKEN ' +
    'или верните DOCX_ANON_HOST=127.0.0.1.'
  );
  process.exit(1);
}

function log(...a) {
  console.log(new Date().toISOString(), ...a);
}

// --- multipart-парсер (без зависимостей) ---
function parseMultipart(buffer, boundary) {
  const delim = Buffer.from(`--${boundary}`);
  const parts = [];
  let start = buffer.indexOf(delim);
  if (start < 0) return parts;
  start += delim.length;
  while (start < buffer.length) {
    const two = buffer.slice(start, start + 2).toString('latin1');
    if (two === '--') break;
    if (two === '\r\n') start += 2;
    const headerEnd = buffer.indexOf('\r\n\r\n', start);
    if (headerEnd < 0) break;
    const headers = buffer.slice(start, headerEnd).toString('utf-8');
    const bodyStart = headerEnd + 4;
    const next = buffer.indexOf(delim, bodyStart);
    const bodyEnd = next < 0 ? buffer.length : next - 2;
    const nameMatch = /name="([^"]*)"/.exec(headers);
    const filenameMatch = /filename="([^"]*)"/.exec(headers);
    parts.push({
      name: nameMatch ? nameMatch[1] : '',
      filename: filenameMatch ? filenameMatch[1] : '',
      data: buffer.slice(bodyStart, bodyEnd),
    });
    if (next < 0) break;
    start = next + delim.length;
  }
  return parts;
}

// --- запуск CLI-обёртки ---
function runWrapper(args, timeoutMs = 300000) {
  return new Promise((res) => {
    const cmd = ['node', WRAPPER, '--repo', REPO, ...args];
    const p = spawn(cmd[0], cmd.slice(1), { stdio: ['ignore', 'pipe', 'pipe'] });
    let out = '';
    let err = '';
    const timer = setTimeout(() => {
      p.kill('SIGKILL');
      res({ code: 124, out, err: 'timeout' });
    }, timeoutMs);
    p.stdout.on('data', (d) => (out += d));
    p.stderr.on('data', (d) => (err += d));
    p.on('close', (code) => {
      clearTimeout(timer);
      res({ code: code ?? 1, out, err });
    });
  });
}

// --- текст из docx (чистый Node, без python: fflate из node_modules) ---
async function docxText(bytes) {
  const req = await import('node:module').then((m) => m.createRequire(join(REPO, 'package.json')));
  const { unzipSync } = req('fflate');
  const files = unzipSync(new Uint8Array(bytes));
  const parts = [];
  for (const name of Object.keys(files)) {
    if (!name.endsWith('.xml')) continue;
    const xml = Buffer.from(files[name]).toString('utf-8');
    // Теги с namespace-префиксом: <w:t>, <a:t> и т.п.
    const re = /<([A-Za-z0-9_]+):t(?:\s[^>]*)?>([\s\S]*?)<\/\1:t>/g;
    let m;
    while ((m = re.exec(xml)) !== null) {
      const text = m[2]
        .replace(/<[^>]+>/g, '')
        .replace(/&lt;/g, '<')
        .replace(/&gt;/g, '>')
        .replace(/&quot;/g, '"')
        .replace(/&apos;/g, "'")
        .replace(/&amp;/g, '&');
      if (text) parts.push(text);
    }
  }
  return parts.join(' ');
}

// --- основная операция ---
// options — переопределения настроек ядра для этого запроса:
//   commercial          — группа «Коммерческая информация» (bool | undefined)
//   mode                — режим замены: placeholder | mask | delete
//   nerEnabled          — использовать модель NER
//   nerThreshold        — порог NER (0..1)
//   govEntities         — не трогать госорганы (true) / маскировать (false)
//   dictNames           — словарный детектор ФИО третьих лиц
//   keepMetadata        — оставлять метаданные docProps
//   stripEmbedded       — вырезать встроенные OLE-объекты
//   placeholderLanguage — язык меток: document | ru | en
//   language            — язык распознавания: auto | ru | en | both
// undefined — берётся значение по умолчанию (или из окружения сервиса).
async function anonymizeBuffer(bytes, filename, options = {}) {
  const ext = (extname(filename || '') || '.docx').toLowerCase();
  const tmp = await mkdtemp();
  const inPath = join(tmp, `in${ext}`);
  const outPath = join(tmp, 'out.anon');
  const mapPath = join(tmp, 'map.json');
  await writeFile(inPath, bytes);

  const args = ['--in', inPath, '--out', outPath, '--map', mapPath, '--json'];

  // --- коммерческая группа ---
  const useCommercial = options.commercial === undefined
    ? COMMERCIAL
    : !!options.commercial;
  if (useCommercial) args.push('--commercial');
  else args.push('--no-commercial');

  // --- режим замены ---
  const mode = options.mode || MODE;
  args.push('--mode', mode);

  // --- NER ---
  const useNer = options.nerEnabled === undefined
    ? NER_ENABLED
    : !!options.nerEnabled;
  if (options.nerThreshold === undefined && NER_THRESHOLD !== undefined) {
    options.nerThreshold = NER_THRESHOLD;
  }
  if (options.govEntities === undefined && GOV_ENTITIES !== undefined) {
    options.govEntities = GOV_ENTITIES;
  }
  if (options.dictNames === undefined && DICT_NAMES !== undefined) {
    options.dictNames = DICT_NAMES;
  }
  if (options.keepMetadata === undefined && KEEP_METADATA !== undefined) {
    options.keepMetadata = KEEP_METADATA;
  }
  if (options.stripEmbedded === undefined && STRIP_EMBEDDED !== undefined) {
    options.stripEmbedded = STRIP_EMBEDDED;
  }
  if (options.placeholderLanguage === undefined && PLACEHOLDER_LANGUAGE !== undefined) {
    options.placeholderLanguage = PLACEHOLDER_LANGUAGE;
  }
  if (options.language === undefined && LANGUAGE !== undefined) {
    options.language = LANGUAGE;
  }
  if (useNer && NER_DIR && existsSync(NER_DIR)) args.push('--ner', NER_DIR);
  else args.push('--no-ner');
  if (typeof options.nerThreshold === 'number' && Number.isFinite(options.nerThreshold)) {
    args.push('--ner-threshold', String(options.nerThreshold));
  }

  // --- детекторы ---
  if (options.govEntities === false) args.push('--no-gov-entities');
  else if (options.govEntities === true) args.push('--gov-entities');
  if (options.dictNames === false) args.push('--no-dict-names');
  else if (options.dictNames === true) args.push('--dict-names');

  // --- метаданные и встроенные объекты ---
  if (options.keepMetadata === true) args.push('--keep-metadata');
  else if (options.keepMetadata === false) args.push('--no-keep-metadata');
  if (options.stripEmbedded === true) args.push('--strip-embedded');

  // --- языки ---
  if (options.placeholderLanguage) args.push('--placeholder-language', options.placeholderLanguage);
  if (options.language) args.push('--language', options.language);

  const r = await runWrapper(args);
  if (r.code !== 0) {
    const detail = (r.err || r.out || '').trim().split('\n').slice(-3).join(' | ');
    throw new Error(`WRAPPER_EXIT_${r.code}: ${detail}`);
  }
  let map = JSON.parse(await readFile(mapPath, 'utf-8'));
  const outBytes = await readFile(outPath);
  const meta = await docxMeta(outBytes);
  const isZip = outBytes.length > 4 && outBytes[0] === 0x50 && outBytes[1] === 0x4b;
  let text;
  if (isZip) {
    text = await docxText(outBytes);
  } else {
    text = outBytes.toString('utf-8');
  }
  let stats = {};
  try {
    stats = JSON.parse(r.out);
  } catch {}
  await rm(tmp, { recursive: true, force: true });

  // «Госорганы: не трогать» — модель NER маскирует их сама, правило ядра
  // на NER-спаны не действует. Возвращаем госорганы как есть.
  if (options.govEntities === true && map && Array.isArray(map.entries)) {
    const filtered = stripGovEntities(map, text);
    map = filtered.map;
    text = filtered.text;
  }

  return { text, map, stats, isZip, meta };
}

/**
 * Убирает из карты замены госорганов (govEntities=1 → «не трогать»).
 *
 * Ядро применяет govEntities только к правиловому детектору компаний.
 * Модель NER маскирует госорганы сама, поэтому после сборки карты такие
 * записи убираются: оригинал возвращается в текст, а метка исчезает.
 */
/**
 * Метаданные DOCX (docProps/*) из обезличенного файла.
 *
 * Нужны, чтобы переключатель «Метаданные: оставлять/вычищать» был
 * наблюдаемым: в тексте документа метаданных нет, они лежат в отдельных
 * частях пакета OOXML.
 */
async function docxMeta(bytes) {
  if (!bytes || bytes.length < 4) return null;
  if (bytes[0] !== 0x50 || bytes[1] !== 0x4b) return null; // не zip
  try {
    const coreMod = await import(pathToFileURL(join(REPO, 'src/core/index.js')).href);
    const entries = coreMod.readZip(bytes);
    if (!Array.isArray(entries)) return null;
    const meta = {};
    for (const e of entries) {
      if (!e || !/^docProps\/.*\.xml$/i.test(e.name || '')) continue;
      let data;
      try {
        data = coreMod.inflateEntry(e);
      } catch {
        data = e.raw;
      }
      if (!data) continue;
      const xml = Buffer.from(data).toString('utf-8');
      const pick = (tag) => {
        const re = new RegExp('<' + tag + '[^>]*>([\\s\\S]*?)</' + tag + '>', 'i');
        const m = re.exec(xml);
        return m ? m[1].trim() : null;
      };
      const pickLoose = (tag) => {
        const re = new RegExp('<[^>]*' + tag + '[^>]*>([\\s\\S]*?)</[^>]*' + tag + '>', 'i');
        const m = re.exec(xml);
        return m ? m[1].trim() : null;
      };
      meta[e.name] = {
        creator: pickLoose('dc:creator') ?? pickLoose('creator'),
        lastModifiedBy: pickLoose('cp:lastModifiedBy') ?? pickLoose('lastModifiedBy'),
        title: pickLoose('dc:title') ?? pickLoose('title'),
        subject: pickLoose('dc:subject') ?? pickLoose('subject'),
        description: pickLoose('dc:description') ?? pickLoose('description'),
        keywords: pickLoose('cp:keywords') ?? pickLoose('keywords'),
        category: pickLoose('cp:category') ?? pickLoose('category'),
      };
    }
    return Object.keys(meta).length ? meta : null;
  } catch {
    return null;
  }
}

function stripGovEntities(map, text) {
  if (!map || !Array.isArray(map.entries) || !GOV_SET.size) return { map, text };
  const entries = [];
  let out = text ?? '';
  for (const e of map.entries) {
    if (e && e.category === 'COMPANY' && isGovValue(e.value ?? e.original ?? '')) {
      // Госорган: возвращаем оригинал на место метки.
      const placeholder = e.placeholder ?? e.token;
      if (placeholder && typeof out === 'string' && out.includes(placeholder)) {
        out = out.split(placeholder).join(e.original ?? e.value ?? '');
      }
      continue;
    }
    entries.push(e);
  }
  const outMap = { ...map, entries };
  // Пересчитываем счётчики по категориям.
  const byCategory = {};
  for (const e of entries) {
    if (e && e.category) byCategory[e.category] = (byCategory[e.category] || 0) + 1;
  }
  outMap.stats = { ...(map.stats || {}), replacements: entries.length, byCategory };
  return { map: outMap, text: out };
}

/** Название организации — госорган из списка ядра? */
function isGovValue(value) {
  const v = String(value || '').trim().toLowerCase();
  if (!v) return false;
  if (GOV_SET.has(v)) return true;
  // «АО "Сбербанк"» → ищем и полную форму, и ядро
  const quoted = v.replace(/[«»"'()]/g, ' ').trim();
  if (GOV_SET.has(quoted)) return true;
  const withoutForm = quoted.replace(/^(ооо|оао|зао|пао|ао|гк|учреждение|федеральное|государственное|государственное бюджетное учреждение)\s+/i, '').trim();
  return GOV_SET.has(withoutForm);
}

async function mkdtemp() {
  const p = join(tmpdir(), `anon-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`);
  await mkdir(p, { recursive: true });
  return p;
}

// --- восстановление по карте (ядро, с морфологией/склонением) ---
// Ядро само выбирает вариант по контексту («его супруга [ФИО_3]» → «Белоусова»)
// и по роду/падежу. Свой regex этого не умел — путал варианты.
let coreMod = null;
async function getCore() {
  if (!coreMod) {
    coreMod = await import(pathToFileURL(join(REPO, 'src/core/index.js')).href);
  }
  return coreMod;
}

async function restoreFromMap(text, map) {
  const core = await getCore();
  const res = core.restoreText(text, map, { decline: true });
  return { text: res.text ?? res, unresolved: (res.unresolved || []).length };
}

// --- HTTP ---
function json(res, code, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(body);
}

const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);

    // Проверка токена — до любой обработки. /health оставляем открытым:
    // он не содержит ПДн и нужен для проверки живости.
    if (TOKEN && url.pathname !== '/health') {
      const provided = String(req.headers['authorization'] || '')
        .replace(/^Bearer\s+/i, '')
        .trim();
      if (provided !== TOKEN) {
        log('auth failed:', req.method, url.pathname, 'from', req.socket.remoteAddress);
        return json(res, 401, { error: 'UNAUTHORIZED' });
      }
    }

    if (req.method === 'GET' && url.pathname === '/health') {
      return json(res, 200, {
        ok: true,
        ner: !!(NER_DIR && existsSync(NER_DIR)),
        repo: REPO,
        ner_dir: NER_DIR,
        wrapper: WRAPPER,
        port: PORT,
        pid: process.pid,
      });
    }

    if (req.method === 'POST' && url.pathname === '/anonymize') {
      const chunks = [];
      for await (const c of req) chunks.push(c);
      const body = Buffer.concat(chunks);
      const ct = req.headers['content-type'] || '';
      const m = /boundary=(?:"([^"]+)"|([^;]+))/i.exec(ct);
      if (!m) return json(res, 400, { error: 'NO_BOUNDARY' });
      const parts = parseMultipart(body, m[1] || m[2]);
      const file = parts.find((p) => p.name === 'file') || parts[0];
      if (!file || !file.data.length) return json(res, 400, { error: 'NO_FILE' });
      // Настройки обезличивания можно переопределить на каждый запрос:
      //   ?commercial=1&mode=mask&nerEnabled=0&... — из query;
      //   одноимённые поля в multipart — из тела.
      // Не заданное значение — берётся из окружения сервиса (или его умолчания).
      const options = {};

      const asBool = (v) => {
        if (v === null || v === undefined || v === '') return undefined;
        const s = String(v).trim().toLowerCase();
        if (s === '1' || s === 'true' || s === 'yes' || s === 'on' || s === 'да') return true;
        if (s === '0' || s === 'false' || s === 'no' || s === 'off' || s === 'нет') return false;
        return undefined;
      };
      const fieldValue = (name) => {
        const q = url.searchParams.get(name);
        if (q !== null && q !== '') return q;
        const f = parts.find((p) => p.name === name);
        if (f) return f.data.toString('utf-8').trim();
        return null;
      };

      const vCommercial = asBool(fieldValue('commercial'));
      if (vCommercial !== undefined) options.commercial = vCommercial;

      const vMode = fieldValue('mode');
      if (vMode) options.mode = vMode;

      const vNer = asBool(fieldValue('nerEnabled'));
      if (vNer !== undefined) options.nerEnabled = vNer;

      const vThreshold = fieldValue('nerThreshold');
      if (vThreshold !== null && vThreshold !== '') {
        const n = Number(vThreshold);
        if (Number.isFinite(n) && n >= 0 && n <= 1) options.nerThreshold = n;
      }

      const vGov = asBool(fieldValue('govEntities'));
      if (vGov !== undefined) options.govEntities = vGov;

      const vDict = asBool(fieldValue('dictNames'));
      if (vDict !== undefined) options.dictNames = vDict;

      const vMeta = asBool(fieldValue('keepMetadata'));
      if (vMeta !== undefined) options.keepMetadata = vMeta;

      const vStrip = asBool(fieldValue('stripEmbedded'));
      if (vStrip !== undefined) options.stripEmbedded = vStrip;

      const vPhLang = fieldValue('placeholderLanguage');
      if (vPhLang) options.placeholderLanguage = vPhLang;

      const vLang = fieldValue('language');
      if (vLang) options.language = vLang;

      try {
        const r = await anonymizeBuffer(file.data, file.filename, options);
        if (!r.text || !r.text.trim()) {
          return json(res, 422, { error: 'EMPTY_TEXT', detail: 'не удалось извлечь текст' });
        }
        await mkdir(WORKDIR, { recursive: true });
        const id = `${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
        await writeFile(join(WORKDIR, `${id}.map.json`), JSON.stringify(r.map), { mode: 0o600 });
        return json(res, 200, {
          text: r.text,
          map: r.map,
          stats: { ...(r.stats || {}), bytes: file.data.length },
          meta: r.meta,
          id,
        });
      } catch (e) {
        log('anonymize failed:', e.message);
        return json(res, 422, { error: 'ANONYMIZE_FAILED', detail: String(e.message || e) });
      }
    }

    if (req.method === 'POST' && url.pathname === '/extract') {
      // Извлечение текста из офисных форматов, которые не читает ядро:
      // xlsx, pptx, odt, doc. Сделано в сервисе, а не в Open WebUI,
      // чтобы не зависеть от его разборщиков (Pandoc/LibreOffice).
      const chunks = [];
      for await (const c of req) chunks.push(c);
      const body = Buffer.concat(chunks);
      const ct = req.headers['content-type'] || '';
      const m = /boundary=(?:"([^"]+)"|([^;]+))/i.exec(ct);
      if (!m) return json(res, 400, { error: 'NO_BOUNDARY' });
      const parts = parseMultipart(body, m[1] || m[2]);
      const file = parts.find((p) => p.name === 'file') || parts[0];
      if (!file || !file.data.length) return json(res, 400, { error: 'NO_FILE' });
      try {
        const r = await extractOfficeText(file.data, file.filename || '', REPO);
        if (!r || !r.text || !r.text.trim()) {
          return json(res, 422, {
            error: 'EXTRACT_FAILED',
            detail: 'формат не поддерживается или текст не найден',
          });
        }
        return json(res, 200, { text: r.text, kind: r.kind });
      } catch (e) {
        log('extract failed:', e.message);
        return json(res, 422, { error: 'EXTRACT_FAILED', detail: String(e.message || e) });
      }
    }

    if (req.method === 'POST' && url.pathname === '/restore') {
      const chunks = [];
      for await (const c of req) chunks.push(c);
      let payload;
      try {
        payload = JSON.parse(Buffer.concat(chunks).toString('utf-8'));
      } catch {
        return json(res, 400, { error: 'BAD_JSON' });
      }
      const { text, map } = payload;
      if (typeof text !== 'string' || !map) return json(res, 400, { error: 'BAD_PAYLOAD' });
      try {
        const r = await restoreFromMap(text, map);
        return json(res, 200, { text: r.text, unresolved: r.unresolved });
      } catch (e) {
        return json(res, 422, { error: 'RESTORE_FAILED', detail: String(e.message || e) });
      }
    }

    return json(res, 404, { error: 'NOT_FOUND' });
  } catch (e) {
    log('request error:', e);
    return json(res, 500, { error: 'INTERNAL', detail: String(e.message || e) });
  }
});

server.listen(PORT, HOST, () => {
  log(`docx-anonymizer service on ${HOST}:${PORT} repo=${REPO} ner=${NER_DIR}`);
});
