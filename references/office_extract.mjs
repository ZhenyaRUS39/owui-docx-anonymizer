/**
 * Извлечение текста из офисных форматов, которые не читает ядро
 * обезличивания: .xlsx, .pptx, .odt (OpenDocument), .doc (бинарный OLE).
 *
 * Зачем это в сервисе, а не в Open WebUI:
 *   - Open WebUI разбирает .xlsx/.pptx/.odt сам, но .doc требует LibreOffice
 *     (в контейнере его нет), а .odt в этом окружении упирается в сломанный
 *     Pandoc (sandbox-режим не находит data-файлы);
 *   - если извлечение делает сервис, поведение одинаково для всех форматов
 *     и не зависит от версии/настройки Open WebUI.
 *
 * Все форматы — это либо ZIP-контейнеры (xlsx/pptx/odt), либо OLE2 (.doc).
 * Читаем их напрямую: fflate из node_modules upstream-репозитория.
 */
import { join } from 'node:path';
import { createRequire } from 'node:module';

let unzipSync = null;

async function ensureUnzip(repoDir) {
  if (unzipSync) return unzipSync;
  const req = createRequire(join(repoDir, 'package.json'));
  ({ unzipSync } = req('fflate'));
  return unzipSync;
}

function decodeEntities(s) {
  return s
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(Number(d)))
    .replace(/&amp;/g, '&');
}

/** Текст из XML-подобной строки: выкидываем теги, разбираем сущности.
 *  Переводы строк сохраняем — по ним восстанавливаем абзацы. */
function xmlText(xml) {
  return decodeEntities(
    xml
      .replace(/<[^>]+>/g, ' ')
      .replace(/[ \t]+/g, ' ')
      .replace(/ *\n */g, '\n')
      .replace(/\n{3,}/g, '\n\n')
      .trim()
  );
}

/**
 * Текст из OOXML-документа (docx/xlsx/pptx): собираем текстовые узлы.
 * У xlsx текст лежит в <t> внутри sharedStrings и в inlineStr, у pptx — в
 * <a:t> внутри слайдов и заметок, у docx — в <w:t>.
 */
function ooxmlText(files, tags) {
  const parts = [];
  for (const name of Object.keys(files)) {
    if (!name.endsWith('.xml')) continue;
    const xml = Buffer.from(files[name]).toString('utf-8');
    for (const tag of tags) {
      const re = new RegExp(`<${tag}(?:\\s[^>]*)?>([\\s\\S]*?)</${tag}>`, 'g');
      let m;
      while ((m = re.exec(xml)) !== null) {
        const t = xmlText(m[1]);
        if (t) parts.push(t);
      }
    }
  }
  // Каждая ячейка/значение — своя строка: склейка пробелом ломает разбор
  // (значения из разных ячеек сливаются, ядро не находит сущность).
  return parts.join('\n').replace(/[ \t]+/g, ' ').trim();
}

/**
 * XLSX: значения ячеек. Числа и даты не несут ПДн, но их тоже полезно
 * видеть модели как контекст — оставляем как есть.
 * Учитываем sharedStrings (общие строки) и inlineStr (строки прямо в ячейке).
 */
function xlsxText(files) {
  const shared = [];
  if (files['xl/sharedStrings.xml']) {
    const xml = Buffer.from(files['xl/sharedStrings.xml']).toString('utf-8');
    const re = /<si(?:\s[^>]*)?>([\s\S]*?)<\/si>/g;
    let m;
    while ((m = re.exec(xml)) !== null) {
      const t = xmlText(m[1]);
      shared.push(t);
    }
  }
  const rows = [];
  for (const name of Object.keys(files)) {
    if (!/^xl\/worksheets\/.*\.xml$/.test(name)) continue;
    const xml = Buffer.from(files[name]).toString('utf-8');
    const rowRe = /<row(?:\s[^>]*)?>([\s\S]*?)<\/row>/g;
    let rm;
    while ((rm = rowRe.exec(xml)) !== null) {
      const cells = [];
      const cellRe = /<c(\s[^>]*)?>([\s\S]*?)<\/c>/g;
      let cm;
      while ((cm = cellRe.exec(rm[1])) !== null) {
        // Атрибуты ячейки (r=, t=) — в открывающем теге, а не в содержимом.
        const attrs = cm[1] || '';
        const cellXml = cm[2];
        const tMatch = /<t(?:\s[^>]*)?>([\s\S]*?)<\/t>/.exec(cellXml);
        if (tMatch) {
          cells.push(xmlText(tMatch[1]));
          continue;
        }
        const vMatch = /<v(?:\s[^>]*)?>([\s\S]*?)<\/v>/.exec(cellXml);
        if (vMatch) {
          const raw = decodeEntities(vMatch[1]).trim();
          const isShared = /\bt="s"/.test(attrs);
          if (isShared) {
            const idx = Number(raw);
            if (Number.isInteger(idx) && idx >= 0 && idx < shared.length) {
              cells.push(shared[idx]);
            }
          } else if (raw) {
            cells.push(raw);
          }
        }
      }
      const line = cells.filter((c) => c !== '').join(' | ');
      if (line) rows.push(line);
    }
  }
  return rows.join('\n').trim();
}

/** PPTX: текст слайдов и заметок докладчика. */
function pptxText(files) {
  const slides = [];
  const names = Object.keys(files)
    .filter((n) => /^ppt\/(slides|notesSlides)\/.*\.xml$/.test(n))
    .sort();
  for (const name of names) {
    const xml = Buffer.from(files[name]).toString('utf-8');
    // Абзац — <a:p>, внутри — runs <a:t>. Границы абзацев сохраняем:
    // без них «выдан 12.05.2015 Зарегистрирован по адресу: ...» склеивается
    // в одну строку, и ядро не находит адрес (его поглощает спан паспорта).
    const paras = [];
    const pre = /<a:p(?:\s[^>]*)?>([\s\S]*?)<\/a:p>/g;
    let pm;
    while ((pm = pre.exec(xml)) !== null) {
      const runs = [];
      const re = /<a:t(?:\s[^>]*)?>([\s\S]*?)<\/a:t>/g;
      let m;
      while ((m = re.exec(pm[1])) !== null) {
        const t = xmlText(m[1]);
        if (t) runs.push(t);
      }
      if (runs.length) paras.push(runs.join(''));
    }
    if (!paras.length) {
      // Слайд без <a:p> (бывает в сырых файлах) — берём все <a:t>.
      const parts = [];
      const re = /<a:t(?:\s[^>]*)?>([\s\S]*?)<\/a:t>/g;
      let m;
      while ((m = re.exec(xml)) !== null) {
        const t = xmlText(m[1]);
        if (t) parts.push(t);
      }
      if (parts.length) paras.push(parts.join(' '));
    }
    if (paras.length) slides.push(paras.join('\n'));
  }
  return slides.join('\n').trim();
}

/**
 * ODT: OpenDocument — ZIP с content.xml. Текст лежит в <text:p>, <text:h>,
 * <text:span> и т.п. Берём всё содержимое content.xml.
 */
function odtText(files) {
  const xml = files['content.xml']
    ? Buffer.from(files['content.xml']).toString('utf-8')
    : '';
  if (!xml) return '';
  // Разделяем абзацы и ячейки таблиц. Важно: НЕ вставлять пробел внутрь
  // открывающего тега — иначе текст абзаца станет частью «тега» и
  // исчезнет при очистке разметки. Поэтому маркер ставим перед
  // ЗАКРЫВАЮЩИМ тегом.
  const marked = xml
    .replace(/<\/text:p>/g, '\n')
    .replace(/<\/text:h>/g, '\n')
    .replace(/<\/table:table-cell>/g, ' | ')
    .replace(/<\/table:table-row>/g, '\n');
  return xmlText(marked)
    .replace(/(\s*\|\s*)+/g, ' | ')
    .replace(/[ \t]+/g, ' ')
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean)
    .join('\n');
}

/**
 * Извлечение текста из .doc через LibreOffice.
 *
 * LibreOffice разбирает все варианты Word 97–2003 (включая таблицы и
 * колонтитулы), поэтому он основной путь. Свой разбор OLE — запасной.
 *
 * Конвертация идёт в отдельном каталоге с профилем пользователя: без
 * этого LibreOffice падает, если профиль уже занят другим процессом.
 */
async function docTextViaSoffice(bytes, repoDir) {
  const { spawn } = await import('node:child_process');
  const { mkdtemp, writeFile, readFile, rm, mkdir } = await import('node:fs/promises');
  const { tmpdir } = await import('node:os');
  const { join } = await import('node:path');

  const dir = await mkdtemp(join(tmpdir(), 'docx-'));
  const profile = join(dir, 'profile');
  const out = join(dir, 'out');
  await mkdir(profile, { recursive: true });
  await mkdir(out, { recursive: true });
  const src = join(dir, 'in.doc');
  await writeFile(src, bytes);

  const soffice = process.env.DOCX_ANON_SOFFICE || 'soffice';
  const args = [
    '--headless', '--norestore', '--invisible', '--nolockcheck', '--nodefault',
    `-env:UserInstallation=file://${profile}`,
    '--convert-to', 'txt:Text (encoded):UTF8',
    '--outdir', out, src,
  ];

  let code = null;
  try {
    code = await new Promise((resolve) => {
      const p = spawn(soffice, args, { stdio: ['ignore', 'pipe', 'pipe'] });
      let err = '';
      p.stderr.on('data', (d) => { err += d.toString(); });
      const timer = setTimeout(() => { try { p.kill('SIGKILL'); } catch {} }, 120000);
      p.on('close', (c) => { clearTimeout(timer); resolve(c); });
      p.on('error', () => { clearTimeout(timer); resolve(null); });
    });
  } catch {
    code = null;
  }
  if (code !== 0) {
    await rm(dir, { recursive: true, force: true });
    return '';
  }
  let text = '';
  try {
    text = await readFile(join(out, 'in.txt'), 'utf-8');
  } catch {
    text = '';
  }
  await rm(dir, { recursive: true, force: true });
  // LibreOffice пишет BOM — убираем.
  return text.replace(/^\uFEFF/, '').trim();
}

/**
 * DOC (бинарный OLE2): два пути.
 *
 * 1) LibreOffice (soffice --convert-to txt) — надёжный путь: понимает все
 *    варианты .doc (Word 97–2003), таблицы, колонтитулы. Требует установки
 *    libreoffice-writer в образ сервиса.
 * 2) Собственный разбор OLE2 + piece table (FIB/CLX/PCD) — запасной путь,
 *    если LibreOffice недоступен. Работает на простых .doc.
 *
 * Выбор: сначала LibreOffice, при неудаче — свой разбор.
 */
function oleText(bytes) {
  // --- заголовок OLE2 ---
  if (bytes.length < 512) return '';
  if (!(bytes[0] === 0xd0 && bytes[1] === 0xcf)) return '';
  // Смещения полей заголовка OLE2:
  //   0x1e — sector shift (размер сектора = 1 << shift)
  //   0x20 — mini sector shift
  //   0x2c — число FAT-секторов
  //   0x30 — номер первого сектора каталога
  //   0x38 — порог мини-потока (mini stream cutoff)
  //   0x3c — начало miniFAT
  //   0x44 — начало DIFAT
  const sectorSize = 1 << bytes.readUInt16LE(0x1e);
  const miniSectorSize = 1 << bytes.readUInt16LE(0x20);
  const numFatSectors = bytes.readUInt32LE(0x2c);
  const dirStart = bytes.readUInt32LE(0x30);
  const miniCutoff = bytes.readUInt32LE(0x38);
  const miniFatStart = bytes.readUInt32LE(0x3c);
  const difatStart = bytes.readUInt32LE(0x44);

  const sectorOffset = (n) => 512 + n * sectorSize;

  // --- FAT ---
  const fat = [];
  const difat = [];
  for (let i = 0; i < 109 && difat.length < numFatSectors; i++) {
    const v = bytes.readUInt32LE(0x4c + i * 4);
    if (v === 0xffffffff) break;
    difat.push(v);
  }
  // дополнительные DIFAT-секторы (цепочка)
  let ds = difatStart;
  let guard = 0;
  while (ds !== 0xfffffffe && ds !== 0xffffffff && guard++ < 1024) {
    const off = sectorOffset(ds);
    const perSector = sectorSize / 4 - 1;
    for (let i = 0; i < perSector && fat.length < numFatSectors * sectorSize / 4; i++) {
      const v = bytes.readUInt32LE(off + i * 4);
      if (v === 0xffffffff) break;
      difat.push(v);
    }
    ds = bytes.readUInt32LE(off + perSector * 4);
  }
  for (const fs of difat) {
    const off = sectorOffset(fs);
    for (let i = 0; i < sectorSize / 4; i++) {
      fat.push(bytes.readUInt32LE(off + i * 4));
    }
  }
  if (!fat.length) return '';

  // --- цепочка секторов ---
  const chain = (start) => {
    const out = [];
    let s = start;
    let guard = 0;
    while (s !== 0xfffffffe && s !== 0xffffffff && s < fat.length && guard++ < 1 << 20) {
      out.push(s);
      s = fat[s];
    }
    return out;
  };
  const readChain = (start, size) => {
    const parts = [];
    for (const s of chain(start)) {
      parts.push(bytes.subarray(sectorOffset(s), sectorOffset(s) + sectorSize));
    }
    const buf = Buffer.concat(parts);
    return size ? buf.subarray(0, size) : buf;
  };

  // --- каталог ---
  const dirBytes = readChain(dirStart);
  const entries = [];
  for (let i = 0; i < dirBytes.length; i += 128) {
    const e = dirBytes.subarray(i, i + 128);
    if (e.length < 128) break;
    const nameLen = e.readUInt16LE(0x40);
    if (nameLen < 2) continue;
    let name = e.subarray(0, nameLen - 2).toString('utf-16le');
    const type = e[0x42];
    const start = e.readUInt32LE(0x74);
    const size = e.readUInt32LE(0x78);
    entries.push({ name, type, start, size });
  }

  // --- мини-поток ---
  // Потоки меньше 4096 байт лежат не в FAT, а в мини-потоке Root Entry
  // (обычные сектора), адресуемом через miniFAT секторами по 64 байта.
  const rootEntry = entries.find((e) => e.type === 5);
  let miniStream = null;
  let minifat = [];
  if (rootEntry && miniFatStart !== 0xfffffffe && miniFatStart !== 0xffffffff) {
    const mfBytes = readChain(miniFatStart);
    for (let i = 0; i + 4 <= mfBytes.length; i += 4) {
      minifat.push(mfBytes.readUInt32LE(i));
    }
    if (rootEntry.size > 0 && rootEntry.start < 0xfffffffe) {
      miniStream = readChain(rootEntry.start, rootEntry.size);
    }
  }
  const readMiniChain = (start, size) => {
    if (!miniStream) return Buffer.alloc(0);
    const parts = [];
    let s = start;
    let guard = 0;
    while (s !== 0xfffffffe && s !== 0xffffffff && s < minifat.length && guard++ < 1 << 20) {
      parts.push(miniStream.subarray(s * miniSectorSize, (s + 1) * miniSectorSize));
      s = minifat[s];
    }
    const buf = Buffer.concat(parts);
    return size ? buf.subarray(0, size) : buf;
  };
  const readStream = (entry) => {
    if (entry.size < miniCutoff && entry.type === 2) {
      return readMiniChain(entry.start, entry.size);
    }
    return readChain(entry.start, entry.size);
  };

  const wd = entries.find((e) => e.name === 'WordDocument');
  if (!wd) return '';

  const wdBuf = readStream(wd);
  if (wdBuf.length < 0x200) return '';

  // --- FIB ---
  // FIB (File Information Block) в начале WordDocument:
  //   0x00 — wIdent (0xA5EC)
  //   0x0a — флаги; бит 9 (fWhichTblStm) выбирает поток 0Table/1Table
  //   0x1a — начало FibRgW97 (база для парсинга fibRgFcLcb)
  // В FibRgFcLcb97 поле fcClx имеет номер 66 (0x42), то есть смещение
  // 0x1a + 0x42*4 = 0x1a2, и lcbClx — следующие 4 байта.
  const flags = wdBuf.readUInt16LE(0x0a);
  const whichTbl = (flags >> 9) & 1; // 0 → 0Table, 1 → 1Table
  const fcClx = wdBuf.readUInt32LE(0x1a2);
  const lcbClx = wdBuf.readUInt32LE(0x1a6);

  const tblEntry = entries.find((e) => e.name === '1Table')
                || entries.find((e) => e.name === '0Table');
  if (!tblEntry) return '';
  const tblBuf = readStream(tblEntry);
  if (fcClx + lcbClx > tblBuf.length) return '';

  // --- CLX: piece table ---
  const clx = tblBuf.subarray(fcClx, fcClx + lcbClx);
  let p = 0;
  while (p < clx.length && clx[p] === 0x01) {
    const cb = clx.readUInt16LE(p + 1);
    p += 3 + cb;
  }
  if (p >= clx.length || clx[p] !== 0x02) return '';
  const lcbPlcfpcd = clx.readUInt32LE(p + 1);
  const plc = clx.subarray(p + 5, p + 5 + lcbPlcfpcd);
  const n = Math.floor((lcbPlcfpcd - 4) / 12);
  if (n <= 0) return '';

  const cps = [];
  for (let i = 0; i <= n; i++) cps.push(plc.readUInt32LE(i * 4));
  const pcdOff = (n + 1) * 4;
  const pieces = [];
  for (let i = 0; i < n; i++) {
    const pcd = plc.subarray(pcdOff + i * 8, pcdOff + i * 8 + 8);
    const fc = pcd.readUInt32LE(2);
    const compressed = (fc & 0x40000000) !== 0;
    const fcReal = compressed ? (fc & 0x3fffffff) / 2 : fc & 0x3fffffff;
    pieces.push({ cpStart: cps[i], cpEnd: cps[i + 1], fc: fcReal, compressed });
  }

  // --- текст по кускам ---
  let out = '';
  for (const pc of pieces) {
    const chars = pc.cpEnd - pc.cpStart;
    if (chars <= 0) continue;
    if (pc.compressed) {
      // cp1252, 1 байт на символ
      const start = pc.fc;
      const buf = wdBuf.subarray(start, start + chars);
      out += buf.toString('latin1');
    } else {
      // UTF-16LE, 2 байта на символ
      const start = pc.fc;
      const buf = wdBuf.subarray(start, start + chars * 2);
      out += buf.toString('utf16le');
    }
  }
  return out
    .replace(/\r/g, '\n')
    // Убираем управляющие символы (кроме перевода строки и табуляции):
    // в .doc встречаются служебные байты, которые ломают вывод.
    // eslint-disable-next-line no-control-regex
    .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f]/g, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

/**
 * Главная точка: извлечь текст из переданных байт по расширению/сигнатуре.
 * Возвращает {text, kind} либо null, если формат не поддержан.
 */
export async function extractOfficeText(bytes, filename, repoDir) {
  const ext = (filename || '').toLowerCase().split('.').pop();
  const isZip = bytes.length > 4 && bytes[0] === 0x50 && bytes[1] === 0x4b;

  if (ext === 'doc') {
    // Сначала LibreOffice (надёжно), при неудаче — свой разбор OLE.
    let t = '';
    try {
      t = await docTextViaSoffice(bytes, repoDir);
    } catch {
      t = '';
    }
    if (!t) {
      try {
        t = oleText(bytes);
      } catch {
        t = '';
      }
    }
    return t ? { text: t, kind: 'doc' } : null;
  }
  if (!isZip) return null;

  const unzip = await ensureUnzip(repoDir);
  let files;
  try {
    files = unzip(new Uint8Array(bytes));
  } catch {
    return null;
  }
  const names = Object.keys(files);

  if (ext === 'xlsx' || names.some((n) => /^xl\/workbook\.xml$/.test(n))) {
    const t = xlsxText(files);
    return t ? { text: t, kind: 'xlsx' } : null;
  }
  if (ext === 'pptx' || names.some((n) => /^ppt\/presentation\.xml$/.test(n))) {
    const t = pptxText(files);
    return t ? { text: t, kind: 'pptx' } : null;
  }
  if (ext === 'odt' || names.includes('content.xml')) {
    const t = odtText(files);
    return t ? { text: t, kind: 'odt' } : null;
  }
  return null;
}

export const __testing = { oleText, xlsxText, pptxText, odtText, ooxmlText };
