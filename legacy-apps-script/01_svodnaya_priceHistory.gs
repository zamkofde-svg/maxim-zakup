// Источник: Apps Script проект документа "Сводная"
// https://docs.google.com/spreadsheets/d/1OM5iT4ZZOHR34b175FQU1KUw6grAJ2S4zEpcckXjC0I
// Назначение: трекинг истории изменений цен в листе "Сводная таблица".
// Вход:  лист "Сводная таблица" (A=товар, B...=поставщики)
// Выход: лист "История изменений" (дата, старая цена, новая цена, товар, поставщик)
//        лист "_СнимокЦен" (служебный, последний снимок)

/**
 * НАСТРОЙКИ
 */
const CFG = {
  summarySheetName: 'Сводная таблица',
  historySheetName: 'История изменений',
  snapshotSheetName: '_СнимокЦен', // служебный лист (можно скрыть)
  headerRow: 1,
  productCol: 1,   // A
  firstPriceCol: 2 // B
};

/**
 * Запуск ОДИН РАЗ:
 * - создаст лист истории (если его нет)
 * - создаст служебный лист снимка
 * - заполнит первый снимок (без записи истории)
 */
function setupPriceHistory() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();

  // Лист истории
  let history = ss.getSheetByName(CFG.historySheetName);
  if (!history) {
    history = ss.insertSheet(CFG.historySheetName);
  }
  if (history.getLastRow() === 0) {
    history.getRange(1, 1, 1, 5).setValues([[
      'Дата и время',
      'Старая цена',
      'Новая цена',
      'Товар',
      'Поставщик'
    ]]);
    history.getRange('A:A').setNumberFormat('dd.MM.yyyy HH:mm:ss');
    history.setFrozenRows(1);
  }

  // Служебный лист снимка
  let snap = ss.getSheetByName(CFG.snapshotSheetName);
  if (!snap) {
    snap = ss.insertSheet(CFG.snapshotSheetName);
    snap.hideSheet();
  }

  // Заполняем снимок текущими данными (без логирования)
  writeSnapshotFromSummary_(ss);

  Logger.log('Готово: история и снимок созданы.');
}

/**
 * Основная функция:
 * - сравнивает текущие цены с прошлым снимком
 * - пишет изменения в "История изменений"
 * - обновляет снимок
 *
 * Эту функцию нужно повесить на триггер по времени.
 */
function trackPriceChanges() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(30000)) {
    return; // если другой запуск еще идет
  }

  try {
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const summary = ss.getSheetByName(CFG.summarySheetName);
    if (!summary) throw new Error(`Лист "${CFG.summarySheetName}" не найден`);

    let history = ss.getSheetByName(CFG.historySheetName);
    if (!history) {
      history = ss.insertSheet(CFG.historySheetName);
      history.getRange(1, 1, 1, 5).setValues([[
        'Дата и время',
        'Старая цена',
        'Новая цена',
        'Товар',
        'Поставщик'
      ]]);
      history.getRange('A:A').setNumberFormat('dd.MM.yyyy HH:mm:ss');
      history.setFrozenRows(1);
    }

    let snap = ss.getSheetByName(CFG.snapshotSheetName);
    if (!snap) {
      // Если снимка нет — создаем первый снимок и выходим (без истории)
      snap = ss.insertSheet(CFG.snapshotSheetName);
      snap.hideSheet();
      writeSnapshotFromSummary_(ss);
      return;
    }

    // Текущие данные (карта)
    const currentMap = buildCurrentPriceMap_(summary);

    // Старый снимок (карта)
    const prevMap = readSnapshotMap_(snap);

    // Сравнение
    const now = new Date();
    const rowsToAppend = [];

    for (const key in currentMap) {
      const cur = currentMap[key];
      const prev = prevMap[key];

      // Если раньше этой пары товар+поставщик не было — просто считаем новой базой, не логируем
      if (!prev) continue;

      // Сравниваем цены
      if (!valuesEqual_(prev.price, cur.price)) {
        // Можно пропускать пустые/некорректные значения:
        // if (cur.price === '') continue;

        rowsToAppend.push([
          now,
          prev.price,
          cur.price,
          cur.product,
          cur.supplier
        ]);
      }
    }

    // Добавляем историю
    if (rowsToAppend.length > 0) {
      const startRow = history.getLastRow() + 1;
      history.getRange(startRow, 1, rowsToAppend.length, 5).setValues(rowsToAppend);
      history.getRange(startRow, 1, rowsToAppend.length, 1).setNumberFormat('dd.MM.yyyy HH:mm:ss');
    }

    // Перезаписываем снимок
    writeSnapshotMap_(snap, currentMap);

  } finally {
    lock.releaseLock();
  }
}

/**
 * Создать триггер (запуск каждые 5 минут).
 * Запустить ОДИН РАЗ вручную.
 */
function createPriceHistoryTrigger() {
  // Чтобы не плодить дубли — удалим старые триггеры для этой функции
  deletePriceHistoryTriggers();

  ScriptApp.newTrigger('trackPriceChanges')
    .timeBased()
    .everyMinutes(5)
    .create();

  Logger.log('Триггер создан: каждые 5 минут');
}

/**
 * Удалить триггеры этой задачи (если нужно пересоздать).
 */
function deletePriceHistoryTriggers() {
  const triggers = ScriptApp.getProjectTriggers();
  for (const t of triggers) {
    if (t.getHandlerFunction() === 'trackPriceChanges') {
      ScriptApp.deleteTrigger(t);
    }
  }
}

/* =========================
   ВНУТРЕННИЕ ФУНКЦИИ
   ========================= */

/**
 * Собирает карту текущих цен из листа "Сводная таблица"
 * Ключ: product + separator + supplier
 */
function buildCurrentPriceMap_(summarySheet) {
  const lastRow = summarySheet.getLastRow();
  const lastCol = summarySheet.getLastColumn();

  const map = {};

  if (lastRow < 2 || lastCol < CFG.firstPriceCol) {
    return map;
  }

  // Заголовки поставщиков (строка 1, начиная с B)
  const suppliers = summarySheet
    .getRange(CFG.headerRow, CFG.firstPriceCol, 1, lastCol - CFG.firstPriceCol + 1)
    .getValues()[0];

  // Вся таблица данных (начиная со 2 строки, колонки A:lastCol)
  const data = summarySheet
    .getRange(2, 1, lastRow - 1, lastCol)
    .getValues();

  for (let r = 0; r < data.length; r++) {
    const row = data[r];
    const product = String(row[0] ?? '').trim(); // колонка A

    if (!product) continue;

    for (let c = CFG.firstPriceCol - 1; c < lastCol; c++) {
      const supplier = String(suppliers[c - (CFG.firstPriceCol - 1)] ?? '').trim();
      if (!supplier) continue;

      const rawPrice = row[c];
      const price = normalizeValue_(rawPrice);

      const key = makeKey_(product, supplier);

      map[key] = {
        product: product,
        supplier: supplier,
        price: price
      };
    }
  }

  return map;
}

/**
 * Пишет текущий снимок в служебный лист на основе "Сводная таблица"
 */
function writeSnapshotFromSummary_(ss) {
  const summary = ss.getSheetByName(CFG.summarySheetName);
  const snap = ss.getSheetByName(CFG.snapshotSheetName);

  const currentMap = buildCurrentPriceMap_(summary);
  writeSnapshotMap_(snap, currentMap);
}

/**
 * Читает снимок из служебного листа в карту
 */
function readSnapshotMap_(snapSheet) {
  const map = {};
  const lastRow = snapSheet.getLastRow();

  if (lastRow < 2) return map;

  const values = snapSheet.getRange(2, 1, lastRow - 1, 3).getValues();
  // Колонки: A=Товар, B=Поставщик, C=Цена

  for (const row of values) {
    const product = String(row[0] ?? '').trim();
    const supplier = String(row[1] ?? '').trim();
    const price = normalizeValue_(row[2]);

    if (!product || !supplier) continue;

    const key = makeKey_(product, supplier);
    map[key] = { product, supplier, price };
  }

  return map;
}

/**
 * Перезаписывает служебный лист снимка из карты
 */
function writeSnapshotMap_(snapSheet, priceMap) {
  snapSheet.clearContents();

  // Заголовки
  snapSheet.getRange(1, 1, 1, 3).setValues([['Товар', 'Поставщик', 'Цена']]);

  const rows = [];
  for (const key in priceMap) {
    const item = priceMap[key];
    rows.push([item.product, item.supplier, item.price]);
  }

  if (rows.length > 0) {
    snapSheet.getRange(2, 1, rows.length, 3).setValues(rows);
  }
}

/**
 * Нормализация значения (число/текст/пусто)
 */
function normalizeValue_(v) {
  if (v === null || v === undefined || v === '') return '';

  // Если уже число
  if (typeof v === 'number') {
    return Number(v);
  }

  // Если строка
  const s = String(v).trim();
  if (!s) return '';

  // Пытаемся преобразовать "12,34" -> 12.34
  const maybeNum = Number(s.replace(',', '.'));
  if (!isNaN(maybeNum)) return maybeNum;

  return s; // на случай текстовых значений
}

/**
 * Сравнение значений (включая пустые)
 */
function valuesEqual_(a, b) {
  // Одинаковые типы/значения
  if (a === b) return true;

  // Сравнение чисел с защитой от микро-ошибок
  if (typeof a === 'number' && typeof b === 'number') {
    return Math.abs(a - b) < 1e-9;
  }

  return false;
}

/**
 * Ключ для пары товар+поставщик
 */
function makeKey_(product, supplier) {
  return product + '¦' + supplier;
}
