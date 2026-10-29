export const defaults = { tMin: 15, tMax: 25, hMin: 30, hMax: 60 };

export function emptyState() {
  return {
    climate: { t: null, h: null, age: null }, device: 'unknown',
    products: [], pending: [], scans: [], settings: { ...defaults },
  };
}

export function csvCell(value) {
  if (value == null) return '';
  let text = String(value);
  // Names and tag descriptions must not become formulas in spreadsheet apps.
  if (typeof value === 'string' && /^[\x00-\x20]*[=+\-@]/.test(text)) text = "'" + text;
  return /[",\r\n]/.test(text) ? '"' + text.replaceAll('"', '""') + '"' : text;
}

export function recordsCsv(kind, rows) {
  const headers = kind === 'readings'
    ? ['time_utc', 'temperature_c', 'humidity_pct']
    : ['time_utc', 'kind', 'tag', 'item', 'stock_after'];
  const values = rows.map(row => kind === 'readings'
    ? [new Date(Number(row.ts) * 1000).toISOString(), row.temp, row.hum]
    : [new Date(Number(row.ts) * 1000).toISOString(), row.kind, row.uid, row.name, row.stock_after]);
  return [headers, ...values].map(row => row.map(csvCell).join(',')).join('\r\n') + '\r\n';
}

export function createStore(client) {
  async function rpc(name, args) {
    const { data, error } = await client.rpc(name, args);
    if (error) throw error;
    return data;
  }
  return {
    state: () => rpc('get_state'),
    history: hours => rpc('get_history', { p_hours: Number(hours) }),
    manage: (action, data) => rpc('manage_stock', { p_action: action, p_data: data }),
    async records(kind, isCurrent = () => true) {
      if (!['readings', 'scans'].includes(kind)) throw new Error('Unknown export');
      const key = kind === 'readings' ? 'ts' : 'id';
      const columns = kind === 'readings' ? 'ts,temp,hum' : 'id,ts,kind,uid,name,stock_after';
      const { data: newest, error: newestError } = await client.from(kind)
        .select(key).order(key, { ascending: false }).limit(1);
      if (newestError) throw newestError;
      if (!isCurrent()) throw new Error('Export cancelled');
      if (!newest.length) return [];
      const maximum = newest[0][key];
      let cursor;
      const rows = [];
      // Cursor pagination avoids skipped records when old readings are pruned
      // during an export. The upper bound excludes new events arriving later.
      while (true) {
        let query = client.from(kind).select(columns).lte(key, maximum)
          .order(key, { ascending: true }).limit(500);
        if (cursor !== undefined) query = query.gt(key, cursor);
        const { data, error } = await query;
        if (error) throw error;
        if (!isCurrent()) throw new Error('Export cancelled');
        if (!data.length) break;
        rows.push(...data);
        const next = data.at(-1)[key];
        if (cursor !== undefined && Number(next) <= Number(cursor)) throw new Error('Export did not advance');
        cursor = next;
        if (Number(cursor) >= Number(maximum)) break;
      }
      return rows;
    },
  };
}
