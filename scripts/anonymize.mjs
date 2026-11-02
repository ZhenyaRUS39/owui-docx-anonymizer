#!/usr/bin/env node
// Обёртка ядра docx-anonymizer для серверного/CLI использования (Node, без браузера).
//
// Зачем: src/core/ расширения — чистый JS без chrome.* и без сети, поэтому тот же движок
// обезличивания работает в Node-сервисе, n8n Code-node, MCP-сервере и в Open WebUI Pipe.
//
// Использование:
//   node anonymize.mjs --in <файл> [--out <файл>] [--map <файл.json>] [--restore <файл>] [--map-in <файл.json>]
//                       [--repo <путь к docx-anonymizer>] [--commercial] [--mode placeholder|mask|delete]
//                       [--ner <каталог модели>] [--json]
//
// Что делает:
//   --in + --out : разбирает документ, находит ПДн и идентификаторы, записывает обезличенную копию + карту замен.
//   --restore    : возвращает настоящие значения в обезличенный документ по карте (обратный проход).
//   --json       : печатает статистику в JSON (для агентов и пайплайнов).
//
// Выход: карта замен (mapping.json) — ключевой артефакт. Без неё восстановление невозможно.
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { dirname, join, resolve, extname } from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';

const argv = process.argv.slice(2);
const arg = (name, def = undefined) => {
  if (!name.startsWith('--')) name = `--${name}`;
  const i = argv.indexOf(name);
  if (i === -1) return def;
  const next = argv[i + 1];
  return next && !next.startsWith('--') ? next : true;
};
const flag = (name) => argv.includes(name);

/**
 * Булев флаг трёх состояний: переключатель может быть включён, выключен
 * или не задан (тогда берётся значение по умолчанию).
 *
 * Поддерживаются формы: --flag, --flag=true/false, --flag 0/1,
 * --no-flag (инверсия), --flag true/false (значение отдельным аргументом).
 *
 * Раньше проверка была `arg('--x') === false`, но arg() возвращает строку
 * "false" для "--x false" и true для "--x" — то есть выключение никогда не
 * срабатывало, и настройка молча оставалась включённой.
 */
function boolFlag(name) {
  const plain = name.startsWith('--') ? name : `--${name}`;
  const neg = plain.startsWith('--no-') ? plain : `--no-${plain.replace(/^--/, '')}`;
  if (argv.includes(neg)) return false;
  const i = argv.indexOf(plain);
  if (i === -1) return undefined;
  const raw = argv[i + 1];
  if (raw === undefined || raw.startsWith('--')) return true;
  const v = String(raw).trim().toLowerCase();
  if (v === 'false' || v === '0' || v === 'no' || v === 'off' || v === 'нет') return false;
  if (v === 'true' || v === '1' || v === 'yes' || v === 'on' || v === 'да') return true;
  return true;
}

function die(msg, code = 2) {
  process.stderr.write(`anonymize: ${msg}\n`);
  process.exit(code);
}

/** Рабочий каталог может быть удалён (тесты чистят временные каталоги
 *  между кейсами) — тогда process.cwd() бросает ENOENT. Поэтому берём
 *  каталог скрипта как безопасную базу. */
function safeCwd() {
  try {
    return process.cwd();
  } catch {
    return dirname(fileURLToPath(import.meta.url));
  }
}

/** Корень репозитория docx-anonymizer: --repo, переменная окружения или типовые места. */
function findRepo() {
  const cand = arg('repo', process.env.DOCX_ANON_REPO);
  const list = [
    cand,
    resolve(safeCwd(), 'docx-anonymizer'),
    resolve(safeCwd(), '../docx-anonymizer'),
    '/opt/docx-anonymizer',
    '/srv/docx-anonymizer',
    join(process.env.HOME || '/root', 'docx-anonymizer'),
  ].filter(Boolean);
  for (const p of list) {
    const abs = resolve(String(p));
    if (existsSync(join(abs, 'src', 'core', 'index.js'))) return abs;
  }
  die(
    `не найден исходник docx-anonymizer. Укажи --repo <путь> или DOCX_ANON_REPO.\n` +
      `  Склонировать: git clone https://github.com/argmaster-dev/docx-anonymizer.git`,
  );
}

const repo = findRepo();
const core = await import(pathToFileURL(join(repo, 'src', 'core', 'index.js')).href);
core.registerBuiltinDetectors();

const {
  parseDocx,
  analyze,
  applyDecisions,
  fromText,
  defaultSettings,
  deanonymize,
  restoreDocx,
  restoreText,
  isPdf,
  buildLayout,
} = core;

/** Настройки поверх defaultSettings() — те же поля, что и в расширении. */
function buildSettings() {
  const s = { ...defaultSettings() };

  // --- Коммерческая информация (группа commercial) ---
  const commercial = boolFlag('--commercial');
  if (commercial !== undefined) s.commercial = commercial;

  // --- Режим замены: placeholder | mask | delete ---
  if (arg('--mode')) s.mode = String(arg('--mode'));
  const modeOff = boolFlag('--mode-off');
  if (modeOff === true) s.mode = 'delete';

  // --- Языки ---
  if (arg('--language')) s.language = String(arg('--language'));
  if (arg('--placeholder-language')) s.placeholderLanguage = String(arg('--placeholder-language'));

  // --- Размер файла ---
  if (arg('--max-file-size-mb')) s.maxFileSizeMB = Number(arg('--max-file-size-mb'));

  // --- Метаданные и встроенные объекты ---
  // keepMetadata=true оставляет метаданные docProps; по умолчанию они вычищаются.
  // Флаг --keep-metadata включает оставление, --no-metadata выключает.
  const keepMeta = boolFlag('--keep-metadata');
  if (keepMeta !== undefined) s.keepMetadata = keepMeta;
  // stripEmbedded=true вырезает встроенные OLE-объекты (они не обезличиваются).
  const stripEmb = boolFlag('--strip-embedded');
  if (stripEmb !== undefined) s.stripEmbedded = stripEmb;

  // --- Нейросеть (NER) ---
  const ner = boolFlag('--ner');
  if (ner !== undefined) s.nerEnabled = ner;
  const noNer = boolFlag('--no-ner');
  if (noNer === true) s.nerEnabled = false;
  if (arg('--ner-threshold')) s.nerThreshold = Number(arg('--ner-threshold'));
  if (arg('--ner-threshold-org')) s.nerThresholdOrg = Number(arg('--ner-threshold-org'));
  if (arg('--ner-stoplist')) s.nerStoplist = String(arg('--ner-stoplist')).split(',').map((x) => x.trim()).filter(Boolean);

  // --- Детекторы ---
  // govEntities=true — не трогать госорганы и банки из списка;
  // govEntities=false — маскировать и их тоже.
  const gov = boolFlag('--gov-entities');
  if (gov !== undefined) s.govEntities = gov;
  // dictNames=true — словарный детектор ФИО третьих лиц (уточняет леммы по
  // большому словарю); false — только модель и правила.
  const dict = boolFlag('--dict-names');
  if (dict !== undefined) s.dictNames = dict;

  return s;
}

/** NER-провайдер из каталога модели (onnxruntime-web в Node, WASM, одна нить). */
async function loadNerProvider(dir) {
  const req = await import('node:module').then((m) => m.createRequire(join(repo, 'package.json')));
  const entry = req.resolve('onnxruntime-web').replace(/\.js$/, '.mjs');
  const ort = await import(pathToFileURL(entry).href);
  ort.env.wasm.numThreads = 1;
  ort.env.wasm.proxy = false;
  const { createOnnxNerProvider } = await import(pathToFileURL(join(repo, 'src', 'core', 'ner', 'onnx-provider.js')).href);
  const { readdirSync, readFileSync, statSync } = await import('node:fs');
  const abs = resolve(dir);
  const files = {
    read: async (p) => {
      if (p.includes('/') || p.includes('..')) throw new Error('путь вне пакета');
      return new Uint8Array(readFileSync(join(abs, p)));
    },
    manifest: () => {
      const p = join(abs, 'pack.json');
      if (existsSync(p)) return JSON.parse(readFileSync(p, 'utf8'));
      const base = JSON.parse(readFileSync(join(abs, 'pack.src.json'), 'utf8'));
      const list = readdirSync(abs)
        .filter((n) => n !== 'pack.src.json' && statSync(join(abs, n)).isFile())
        .sort()
        .map((path) => ({ path, sha256: '', size: statSync(join(abs, path)).size }));
      return { ...base, files: list };
    },
  };
  return createOnnxNerProvider({ files, ort, modelId: files.manifest().id });
}

/** PDF → текстовая модель (pdf.js legacy из node_modules, без сети). */
async function pdfToDoc(bytes, name) {
  const pdfjsDir = join(repo, 'node_modules', 'pdfjs-dist');
  const pdfjs = await import(pathToFileURL(join(pdfjsDir, 'legacy', 'build', 'pdf.mjs')).href);
  const { extractPdf } = core;
  // extractPdf сам возвращает layout (с блоками и предупреждениями), buildLayout вызывать не нужно.
  const layout = await extractPdf(pdfjs, bytes, {
    docOptions: {
      cMapUrl: join(pdfjsDir, 'cmaps') + '/',
      cMapPacked: true,
      standardFontDataUrl: join(pdfjsDir, 'standard_fonts') + '/',
    },
  });
  return fromText(layout.blocks, { name, size: bytes.length, format: 'pdf' });
}

/** Текст → текстовая модель (абзацы разделяются пустой строкой). */
function textToDoc(text, name) {
  const blocks = text
    .split(/\n{2,}/)
    .map((t) => t.trim())
    .filter(Boolean)
    .map((t) => ({ kind: 'p', text: t }));
  return fromText(blocks, { name, size: text.length, format: 'text' });
}

async function main() {
  const jsonOut = flag('--json');

  // --- обратный проход: восстановление по карте ---
  const restoreIn = arg('--restore');
  if (restoreIn) {
    const mapPath = arg('--map-in');
    if (!mapPath) die('--restore требует --map-in <карта.json>');
    const mapping = JSON.parse(await readFile(String(mapPath), 'utf8'));
    const bytes = new Uint8Array(await readFile(String(restoreIn)));
    const name = String(restoreIn);
    let out;
    if (isPdf(bytes)) die('восстановление PDF не поддерживается (ядро восстанавливает .docx/.md/.txt)');
    // формат определяем ПО СИГНАТУРЕ, а не по расширению: файл .anon — это docx,
    // и по расширению он уходил в текстовую ветку (двоичный мусор на выходе).
    const isDocx = bytes.length > 4 && bytes[0] === 0x50 && bytes[1] === 0x4b && bytes[2] === 0x03 && bytes[3] === 0x04;
    if (isDocx) {
      out = restoreDocx(bytes, mapping, { decline: !flag('--no-decline') });
    } else {
      const text = new TextDecoder().decode(bytes);
      const res = restoreText(text, mapping, { decline: !flag('--no-decline') });
      out = { bytes: new TextEncoder().encode(res.text ?? res), text: res.text, ...res };
    }
    const dest = arg('--out');
    if (dest) {
      await mkdir(dirname(resolve(String(dest))), { recursive: true });
      await writeFile(String(dest), out.bytes);
    }
    if (jsonOut) process.stdout.write(JSON.stringify({ restored: dest || null, unresolved: out.unresolved?.length ?? 0 }) + '\n');
    else process.stdout.write(`восстановлено → ${dest || '(stdout не задан)'}\n`);
    return;
  }

  const inPath = arg('--in');
  if (!inPath) die('нужен --in <файл>');
  // Признак DOCX: в архиве есть word/document.xml и [Content_Types].xml.
// Имена файлов в ZIP лежат как plain text, поэтому ищем их в буфере без распаковки.
function looksLikeDocx(bytes) {
  const hay = Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength).toString('latin1');
  return hay.includes('word/document.xml') && hay.includes('[Content_Types].xml');
}

const abs = resolve(String(inPath));
  if (!existsSync(abs)) die(`файл не найден: ${abs}`);
  const bytes = new Uint8Array(await readFile(abs));
  const name = abs.split('/').pop();

  // --- разбор документа ---
  let doc;
  // Формат определяем ПО СИГНАТУРЕ, а не по расширению: файл .anon — это docx,
  // и по расширению он отклонялся как "неподдерживаемый формат" (обезличивание
  // обезличенного файла было невозможно).
  const ext = extname(abs).toLowerCase();
  const isZip = bytes.length > 4 && bytes[0] === 0x50 && bytes[1] === 0x4b && bytes[2] === 0x03 && bytes[3] === 0x04;
  const isTextExt = ext === '.txt' || ext === '.md' || ext === '.text';
  if (isPdf(bytes)) {
    doc = await pdfToDoc(bytes, name);
  } else if (isZip) {
    // ZIP — но не обязательно docx: .xlsx/.pptx тоже ZIP. Проверяем содержимое,
    // иначе ошибка приходит из парсера как NOT_DOCX («не найден конец ZIP-каталога»)
    // вместо внятного «неподдерживаемый формат».
    if (!looksLikeDocx(bytes)) {
      die(`неподдерживаемый формат: ${ext || '(без расширения)'} — это ZIP, но не DOCX (нужны .docx/.docm, .pdf с текстовым слоем, .txt/.md)`);
    }
    doc = parseDocx(bytes);
  } else if (isTextExt) {
    doc = textToDoc(new TextDecoder().decode(bytes), name);
  } else if (ext === '.docx' || ext === '.docm') {
    die('файл не является ZIP-архивом (нужны .docx/.docm, .pdf с текстовым слоем, .txt/.md)');
  } else {
    die(`неподдерживаемый формат: ${ext || '(без расширения)'} (нужны .docx/.docm, .pdf с текстовым слоем, .txt/.md)`);
  }

  // --- анализ и применение ---
  const settings = buildSettings();
  const opts = {};
  const nerDir = arg('--ner');
  if (nerDir) opts.nerProvider = await loadNerProvider(String(nerDir));
  const analysis = await analyze(doc, settings, opts);
  const res = applyDecisions(doc, analysis.groups, {
    mode: settings.mode,
    placeholderLanguage: settings.placeholderLanguage === 'document' ? doc.language || 'ru' : settings.placeholderLanguage,
    textFormat: ext === '.txt' ? 'txt' : 'md',
    keepMetadata: settings.keepMetadata,
    stripEmbedded: settings.stripEmbedded,
    createdAt: new Date().toISOString(),
    toolVersion: '0.4.0-node',
  });

  // --- запись результатов ---
  const outPath = arg('--out');
  if (outPath) {
    await mkdir(dirname(resolve(String(outPath))), { recursive: true });
    await writeFile(String(outPath), res.bytes);
  }
  const mapPath = arg('--map');
  if (mapPath) {
    await mkdir(dirname(resolve(String(mapPath))), { recursive: true });
    await writeFile(String(mapPath), JSON.stringify(res.mapping, null, 2));
  }

  const byCategory = {};
  for (const g of analysis.groups) byCategory[g.category] = (byCategory[g.category] || 0) + 1;
  const summary = {
    input: abs,
    output: outPath || null,
    map: mapPath || null,
    format: doc.kind,
    groups: analysis.groups.length,
    replacements: res.stats.replacements,
    byCategory,
    warnings: doc.warnings || [],
  };
  if (jsonOut) process.stdout.write(JSON.stringify(summary, null, 2) + '\n');
  else {
    process.stdout.write(`групп сущностей: ${summary.groups}, замен: ${summary.replacements}\n`);
    process.stdout.write(`категории: ${JSON.stringify(byCategory)}\n`);
    if (summary.output) process.stdout.write(`обезличено → ${summary.output}\n`);
    if (summary.map) process.stdout.write(`карта замен → ${summary.map}\n`);
    for (const w of summary.warnings) process.stdout.write(`предупреждение: ${w.code || ''} ${w.message || ''}\n`);
  }
}

main().catch((e) => {
  if (e && e.code) process.stderr.write(`anonymize: ошибка ${e.code}: ${e.message}\n`);
  else process.stderr.write(`anonymize: ${e?.stack || e}\n`);
  process.exit(1);
});
