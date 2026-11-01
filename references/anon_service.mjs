#!/usr/bin/env node
/**
 * HTTP-сервис обезличивания для Open WebUI (Open WebUI v0.12.0).
 *
 * Зачем сервис, а не node внутри контейнера open-webui:
 *   - в контейнере open-webui нет node вообще (проверено: node/npm отсутствуют);
 *   - SPEC /config/owui_research/SPEC.md §5 прямо рекомендует «call your own
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

const HERE = dirname(fileURLToPath(import.meta.url));
const SKILL_DIR = process.env.DOCX_ANON_SKILL_DIR || dirname(HERE);
const REPO = process.env.DOCX_ANON_REPO || '/config/.hermes/services/docx-anonymizer';
const NER_DIR = process.env.DOCX_ANON_NER || '/config/.hermes/services/ner';
const WRAPPER = process.env.DOCX_ANON_WRAPPER || join(SKILL_DIR, 'scripts', 'anonymize.mjs');
const WORKDIR = process.env.DOCX_ANON_WORKDIR || '/config/.hermes/state/docx-anon';
const PORT = Number(process.env.DOCX_ANON_PORT || 8971);
const HOST = process.env.DOCX_ANON_HOST || '0.0.0.0';
const COMMERCIAL = process.env.DOCX_ANON_COMMERCIAL !== 'false';
const MODE = process.env.DOCX_ANON_MODE || 'placeholder';

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
async function anonymizeBuffer(bytes, filename) {
  const ext = (extname(filename || '') || '.docx').toLowerCase();
  const tmp = await mkdtemp();
  const inPath = join(tmp, `in${ext}`);
  const outPath = join(tmp, 'out.anon');
  const mapPath = join(tmp, 'map.json');
  await writeFile(inPath, bytes);

  const args = ['--in', inPath, '--out', outPath, '--map', mapPath, '--json', '--mode', MODE];
  if (COMMERCIAL) args.push('--commercial');
  if (NER_DIR && existsSync(NER_DIR)) args.push('--ner', NER_DIR);

  const r = await runWrapper(args);
  if (r.code !== 0) {
    const detail = (r.err || r.out || '').trim().split('\n').slice(-3).join(' | ');
    throw new Error(`WRAPPER_EXIT_${r.code}: ${detail}`);
  }
  const map = JSON.parse(await readFile(mapPath, 'utf-8'));
  const outBytes = await readFile(outPath);
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
  return { text, map, stats, isZip };
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
      try {
        const r = await anonymizeBuffer(file.data, file.filename);
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
          id,
        });
      } catch (e) {
        log('anonymize failed:', e.message);
        return json(res, 422, { error: 'ANONYMIZE_FAILED', detail: String(e.message || e) });
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
