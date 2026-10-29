import test from 'node:test';
import assert from 'node:assert/strict';
import { createStore, csvCell, emptyState, recordsCsv } from '../src/store.js';

test('a new session has no records or settings carried over from another user', () => {
  const first = emptyState();
  first.products.push({ name: 'private item' }); first.settings.tMin = 99;
  assert.deepEqual(emptyState().products, []);
  assert.equal(emptyState().settings.tMin, 15);
});

test('CSV exports escape names, newlines, quotes and spreadsheet formulas', () => {
  assert.equal(csvCell('=HYPERLINK("https://example.com")'), `"'=HYPERLINK(""https://example.com"")"`);
  assert.equal(csvCell(-12), '-12');
  assert.equal(csvCell('  +1+1'), "'  +1+1");
  assert.equal(csvCell('two\nlines'), '"two\nlines"');
  assert.equal(csvCell(null), '');
  const csv = recordsCsv('scans', [{ ts: 1700000000, kind: 'manual', uid: '04:A1:B2:C3',
    name: 'Apples, "large"', stock_after: 7 }]);
  assert.match(csv, /^time_utc,kind,tag,item,stock_after\r\n/);
  assert.match(csv, /2023-11-14T22:13:20\.000Z,manual,04:A1:B2:C3,"Apples, ""large""",7\r\n$/);
});

test('the RPC adapter preserves stock operation names and payloads', async () => {
  const calls = [];
  const store = createStore({ rpc: async (...args) => { calls.push(args); return { data: { ok: true } }; } });
  await store.manage('update', { uid: '04:A1:B2:C3', stock: 5 });
  await store.history('24');
  assert.deepEqual(calls, [
    ['manage_stock', { p_action: 'update', p_data: { uid: '04:A1:B2:C3', stock: 5 } }],
    ['get_history', { p_hours: 24 }],
  ]);
});

function exportClient(rows, onPage = () => {}) {
  return { from() {
    let maximum = Infinity, after = -Infinity, descending = false, count = 500;
    const query = {
      select() { return query; },
      order(_key, options) { descending = !options.ascending; return query; },
      limit(value) { count = value; return query; },
      lte(_key, value) { maximum = value; return query; },
      gt(_key, value) { after = value; return query; },
      then(resolve) {
        const data = rows.filter(row => row.ts <= maximum && row.ts > after)
          .sort((a, b) => descending ? b.ts - a.ts : a.ts - b.ts).slice(0, count);
        if (!descending) onPage(data);
        return Promise.resolve({ data }).then(resolve);
      },
    };
    return query;
  } };
}

test('exports page beyond 1,000 rows and exclude readings added after export starts', async () => {
  const rows = Array.from({ length: 1203 }, (_, i) => ({ ts: i + 1, temp: 22, hum: 50 }));
  let pages = 0;
  const store = createStore(exportClient(rows, () => {
    if (++pages === 1) rows.push({ ts: 9999, temp: 30, hum: 60 });
  }));
  const result = await store.records('readings');
  assert.equal(pages, 3);
  assert.equal(result.length, 1203);
  assert.equal(result.at(-1).ts, 1203);
  assert.equal(new Set(result.map(row => row.ts)).size, 1203);
});

test('exports stop when the user signs out instead of downloading a previous session', async () => {
  let current = true;
  const store = createStore(exportClient([{ ts: 1, temp: 22, hum: 50 }], () => { current = false; }));
  await assert.rejects(store.records('readings', () => current), /Export cancelled/);
});

test('RPC failures reach the UI rather than reporting a stock change as saved', async () => {
  const failure = { code: '42501', message: 'Not authorized' };
  const store = createStore({ rpc: async () => ({ data: null, error: failure }) });
  await assert.rejects(store.manage('clear', {}), error => error === failure);
});
