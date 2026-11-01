/**
 * Браузерная проверка фильтра обезличивания в Open WebUI.
 *
 * Работает как настоящий пользователь: Chromium → логин → прикрепляет документ
 * с ПДн → спрашивает модель → проверяет, что видит пользователь.
 *
 *   B1 в DOM чата — восстановленные значения, меток нет
 *   B2 перехваченный POST /api/chat/completions не содержит исходных ПДн
 *   B3 в /tmp/docx-anon/model_input.log (пишет фильтр) — что реально ушло в модель
 */
import { existsSync, readFileSync } from 'node:fs';

// Путь к playwright-core можно переопределить: в разных машинах он лежит
// по-разному (в HA-аддоне /tmp обнуляется при каждом terminal()).
const PLAYWRIGHT_PATH = process.env.PLAYWRIGHT_PATH
  || '/tmp/docx-anonymizer/node_modules/playwright-core/index.mjs';
const { chromium } = await import(PLAYWRIGHT_PATH);

const URL = process.env.OWUI_URL || 'http://192.168.1.4:8080';
// Логин/пароль — только через переменные окружения. В файле их быть не должно.
const EMAIL = process.env.OWUI_EMAIL || '';
const PASSWORD = process.env.OWUI_PASSWORD || '';
const DOC = process.argv[2];
const PROBES = (process.env.PROBES || 'Белоусов,Гусейнова,482913,Молодогвардейская,443010').split(',');

if (!EMAIL || !PASSWORD) {
  console.error('Задай OWUI_EMAIL и OWUI_PASSWORD (переменные окружения).');
  process.exit(2);
}

if (!DOC || !existsSync(DOC)) {
  console.error('Нет файла документа:', DOC);
  process.exit(2);
}

const browser = await chromium.launch({
  executablePath: '/config/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome',
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
});
const ctx = await browser.newContext({ viewport: { width: 1500, height: 1100 } });
const page = await ctx.newPage();

let captured = null;
page.on('request', (req) => {
  if (req.url().includes('/chat/completions') && req.method() === 'POST') {
    try { captured = req.postData() || ''; } catch { captured = ''; }
  }
});
const consoleErrors = [];
page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text().slice(0, 120)); });

const log = (...a) => console.log(...a);

try {
  // ---- логин ----
  await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForTimeout(2500);
  if (await page.locator('input[type="password"]').count()) {
    await page.locator('input[type="email"], input[name="email"]').first().fill(EMAIL);
    await page.locator('input[type="password"]').first().fill(PASSWORD);
    await page.locator('button[type="submit"], button:has-text("Sign in")').first().click();
    await page.waitForTimeout(5000);
  }
  log('логин: ок, URL =', page.url());

  // ---- новый чат ----
  await page.goto(`${URL}/`, { waitUntil: 'domcontentloaded', timeout: 60000 });
  const input = page.locator('#chat-input');
  await input.waitFor({ state: 'visible', timeout: 45000 });
  log('поле ввода: найдено');

  // ---- прикрепление файла (через скрытый input[type=file]) ----
  const fileInputs = page.locator('input[type="file"]');
  const n = await fileInputs.count();
  log('input[type=file] на странице:', n);
  let attached = false;
  for (let i = 0; i < n; i++) {
    try {
      await fileInputs.nth(i).setInputFiles(DOC);
      attached = true;
      log('файл прикреплён через input #' + i);
      break;
    } catch (e) { /* следующий */ }
  }
  if (!attached) {
    // запасной путь: кнопка с выпадающим меню → Attach file
    await page.locator('button').filter({ has: page.locator('svg') }).last().click({ timeout: 10000 }).catch(() => {});
    await page.waitForTimeout(1200);
    const chooser = await Promise.all([
      page.waitForEvent('filechooser', { timeout: 12000 }).catch(() => null),
      page.locator('text=/Attach|Upload|Attach file/i').first().click().catch(() => {}),
    ]);
    if (chooser[0]) { await chooser[0].setFiles(DOC); attached = true; }
  }
  if (!attached) { log('✗ не удалось прикрепить файл'); }
  await page.waitForTimeout(3000);

  // ---- вопрос ----
  await input.click();
  await page.keyboard.type('Перечисли все ФИО и паспортные данные из приложенного документа.');
  await page.waitForTimeout(500);
  await page.keyboard.press('Enter');
  log('запрос отправлен, ждём ответ…');

  // ---- ждём завершения ответа ----
  // Бюджет ожидания: ответ модели на этом стенде занимает 20-60 с.
  // Держим его меньше общего таймаута запуска, иначе процесс убивается
  // до печати результата.
  const start = Date.now();
  const ANSWER_BUDGET_MS = 150000;
  let lastLen = 0;
  let stable = 0;
  while (Date.now() - start < ANSWER_BUDGET_MS) {
    await page.waitForTimeout(2500);
    const txt = await page.locator('body').innerText().catch(() => '');
    const chat = txt.length;
    if (chat === lastLen && chat > 600) { stable++; if (stable >= 3) break; }
    else stable = 0;
    lastLen = chat;
    if (/Белоусов|Гусейнова|ФИО_1|ПАСПОРТ_1/i.test(txt) && stable >= 1) break;
  }
  // Ждём, пока отрисуется блок ответа ассистента (не пустой).
  for (let i = 0; i < 20; i++) {
    await page.waitForTimeout(2000);
    const cnt = await page.locator('div[id^="message-"]').evaluateAll(
      (els) => els.filter((e) => !(e.className || '').toString().includes('user-message')
        && (e.innerText || '').trim().length > 50).length,
    );
    if (cnt > 0) break;
  }
  // Ждём, пока outlet восстановит метки в ответе (он обновляет сообщение
  // через событие chat:outlet → БД → socket). Пока метки на месте — ответ
  // ещё не восстановлен.
  for (let i = 0; i < 30; i++) {
    await page.waitForTimeout(2000);
    const st = await page.locator('div[id^="message-"]').evaluateAll((els) => {
      const blocks = els
        .filter((e) => !(e.className || '').toString().includes('user-message'))
        .map((e) => (e.innerText || '').trim())
        .filter((t) => t.length > 0);
      const txt = blocks.reduce((a, b) => (b.length > a.length ? b : a), '');
      return { marks: (txt.match(/\[(?:ФИО|ПАСПОРТ|АДРЕС)_\d+\]/g) || []).length, len: txt.length };
    });
    if (st.marks === 0 && st.len > 50) break;
  }
  await page.waitForTimeout(2000);

  const bodyText = await page.locator('body').innerText();
  // Ответ ассистента — последний блок с ролью assistant. Метки в сообщении
  // ПОЛЬЗОВАТЕЛЯ ожидаемы (он видит, что отправил), нас интересует ответ модели.
  // Сообщение ассистента — div[id^="message-"] БЕЗ класса "user-message"
  // (у сообщения пользователя класс "flex w-full user-message group").
  const assistantBlocks = await page.locator('div[id^="message-"]').evaluateAll((els) =>
    els
      .filter((e) => !(e.className || '').toString().includes('user-message'))
      .map((e) => (e.innerText || '').trim())
      .filter((t) => t.length > 0),
  );
  // Берём самый длинный блок: в DOM бывают служебные пустые контейнеры,
  // а ответ модели — самый объёмный.
  const assistantText = assistantBlocks.reduce((a, b) => (b.length > a.length ? b : a), '');
  if (captured) { const fs = await import('node:fs'); fs.writeFileSync('/tmp/captured_request.json', captured); }
  await page.screenshot({ path: '/tmp/owui_browser.png' });

  // ---- что реально ушло в модель (лог фильтра) ----
  let modelInput = '';
  try {
    modelInput = readFileSync('/tmp/owui_model_input.log', 'utf-8');
  } catch {}

  log('\n================= ПРОВЕРКА =================');
  let ok = true;

  // B1: в UI пользователь видит восстановленные значения
  const uiLeaks = PROBES.filter((p) => assistantText.includes(p));
  const sideLeaks = PROBES.filter((p) => bodyText.includes(p));
  // Настоящая метка — [ФИО_1] (с номером). В тексте модели может встречаться
  // [ФИО_N] — это описание плейсхолдера, а не метка.
  const uiMarks = (assistantText.match(/\[(?:ФИО|ПАСПОРТ|АДРЕС)_\d+\]/g) || []);
  log(`B1 ответ ассистента: ПДн=${uiLeaks.length ? uiLeaks.join(', ') : 'нет'}  меток=${uiMarks.length}`);
  log(`B1b вся страница (вкл. боковую панель): ПДн=${sideLeaks.length ? sideLeaks.join(', ') : 'нет'}`);
  if (uiLeaks.length && !uiMarks.length) {
    log('   ✓ пользователь видит восстановленные значения');
  } else if (uiMarks.length) {
    ok = false;
    log('   ✗ в UI остались метки — outlet не восстановил');
  } else {
    ok = false;
    log('   ✗ в UI нет ни ПДн, ни меток — ответ не получен?');
  }

  // B2: перехваченный запрос не содержит исходных ПДн
  if (captured) {
    const reqLeaks = PROBES.filter((p) => captured.includes(p));
    log(`B2 POST /chat/completions: ПДн в теле=${reqLeaks.length ? reqLeaks.join(', ') : 'нет'}`);
    if (reqLeaks.length) {
      ok = false;
      log('   ✗ в запросе к бэкенду исходные ПДн');
    } else {
      log('   ✓ в запросе к бэкенду ПДн нет');
    }
  } else {
    log('B2 POST /chat/completions: не перехвачен (проверяем по логу фильтра)');
  }

  // B2b: в запросе не должно быть блока <attached_files> с id файла —
  // по этому id модель вызывает view_file и читает оригинал.
  if (captured) {
    const hasAttached = /<attached_files>/.test(captured);
    log(`B2b блок <attached_files> в запросе: ${hasAttached ? 'ЕСТЬ — риск view_file' : 'нет'}`);
    if (hasAttached) {
      ok = false;
      log('   ✗ id файла остался в теле запроса');
    } else {
      log('   ✓ id файла не передаётся модели');
    }
  }

  // B3: что реально ушло в модель
  if (modelInput) {
    const modelLeaks = PROBES.filter((p) => modelInput.includes(p));
    const modelMarks = (modelInput.match(/\[(?:ФИО|ПАСПОРТ|АДРЕС)_\d+\]/g) || []);
    log(`B3 текст, ушедший в модель: ПДн=${modelLeaks.length ? modelLeaks.join(', ') : 'нет'}  меток=${modelMarks.length}`);
    if (modelLeaks.length) {
      ok = false;
      log('   ✗ в модель ушли настоящие ПДн');
    } else if (!modelMarks.length) {
      ok = false;
      log('   ✗ в модель не ушли ни ПДн, ни метки');
    } else {
      log('   ✓ модель получила только метки');
    }
  } else {
    log('B3 лог фильтра: недоступен с этой машины (проверка на HA)');
  }

  if (consoleErrors.length) {
    log('\nошибки консоли браузера:', consoleErrors.slice(0, 3));
  }
  log('\nИТОГ:', ok ? 'УСПЕХ' : 'ПРОВАЛ');
  await browser.close();
  process.exit(ok ? 0 : 1);
} catch (e) {
  log('ОШИБКА:', e.message);
  await page.screenshot({ path: '/tmp/owui_browser_err.png' }).catch(() => {});
  await browser.close();
  process.exit(3);
}
