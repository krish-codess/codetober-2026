// DuckDB-WASM holding the metadata table, so a selection is summarised in the browser with no
// request to any server. Loaded lazily after the first frame; the points are usable without it.

import * as duckdb from "@duckdb/duckdb-wasm";
import workerUrl from "@duckdb/duckdb-wasm/dist/duckdb-browser-eh.worker.js?url";
import wasmUrl from "@duckdb/duckdb-wasm/dist/duckdb-eh.wasm?url";
import { tableFromArrays } from "apache-arrow";

export interface TokenRow {
  id: number;
  token: string;
  rank: number;
  n_chars: number;
  script: string | null;
  kind: string;
  cluster: string;
}

export interface MetaSummary {
  rank: { min: number; q25: number; median: number; q75: number; max: number } | null;
  meanChars: number | null;
  top: { token: string; rank: number }[];
  ms: number;
}

export interface MetaDb {
  summary(ids: Uint32Array): Promise<MetaSummary>;
  lookup(id: number): Promise<TokenRow | null>;
  search(text: string, limit?: number): Promise<TokenRow[]>;
  close(): Promise<void>;
}

/** Download with real progress, retrying with capped backoff; resolves to the whole body. */
export async function fetchWithProgress(url: string, expectedBytes: number, onProgress: (share: number) => void, attempts = 4): Promise<Uint8Array> {
  let last: unknown;
  for (let attempt = 1; attempt <= attempts; attempt++) {
    try {
      const res = await fetch(url);
      if (!res.ok) {
        last = new Error(`HTTP ${res.status}`);
        if (res.status < 500 && res.status !== 429) break;
      } else if (!res.body) {
        return new Uint8Array(await res.arrayBuffer());
      } else {
        const out = new Uint8Array(expectedBytes);
        const reader = res.body.getReader();
        let got = 0;
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          if (got + value.length > expectedBytes) throw new Error("the file is larger than the manifest says");
          out.set(value, got);
          got += value.length;
          onProgress(got / expectedBytes);
        }
        if (got !== expectedBytes) throw new Error(`the download stopped at ${got} of ${expectedBytes} bytes`);
        return out;
      }
    } catch (e) {
      last = e;
    }
    if (attempt < attempts) await new Promise((r) => setTimeout(r, Math.min(4000, 250 * 2 ** attempt)));
  }
  throw new Error(`Could not download the metadata table: ${(last as Error).message}`);
}

const num = (v: unknown): number => (typeof v === "bigint" ? Number(v) : (v as number));

export async function openMetaDb(url: string, expectedBytes: number, onProgress: (share: number) => void): Promise<MetaDb> {
  const [bytes, db] = await Promise.all([
    fetchWithProgress(url, expectedBytes, onProgress),
    (async () => {
      const db = new duckdb.AsyncDuckDB(new duckdb.VoidLogger(), new Worker(workerUrl));
      await db.instantiate(wasmUrl);
      return db;
    })(),
  ]);
  await db.registerFileBuffer("meta.parquet", bytes);
  const conn = await db.connect();
  // One in-memory table: every later query is a hash join or scan with no I/O at all.
  await conn.query("CREATE TABLE meta AS SELECT * FROM read_parquet('meta.parquet')");
  await db.dropFile("meta.parquet");
  // Values only ever reach SQL as bound parameters of these prepared statements.
  const byId = await conn.prepare("SELECT id, token, rank, n_chars, script, kind, cluster FROM meta WHERE id = ?");
  const byText = await conn.prepare("SELECT id, token, rank, n_chars, script, kind, cluster FROM meta WHERE contains(lower(token), lower(?)) ORDER BY rank LIMIT ?");
  const row = (r: Record<string, unknown>): TokenRow => ({ id: num(r.id), token: String(r.token), rank: num(r.rank), n_chars: num(r.n_chars), script: r.script == null ? null : String(r.script), kind: String(r.kind), cluster: String(r.cluster) });
  let queue: Promise<unknown> = Promise.resolve(); // one statement at a time on the single connection
  const serial = <T>(fn: () => Promise<T>): Promise<T> => {
    const next = queue.then(fn, fn);
    queue = next.catch(() => undefined);
    return next;
  };

  return {
    summary: (ids) =>
      serial(async () => {
        const t0 = performance.now();
        await conn.query("DROP TABLE IF EXISTS sel");
        await conn.insertArrowTable(tableFromArrays({ id: ids.length ? ids : new Uint32Array(0) }), { name: "sel" });
        const agg = (await conn.query("SELECT min(rank) AS lo, quantile_cont(rank, 0.25) AS q25, median(rank) AS med, quantile_cont(rank, 0.75) AS q75, max(rank) AS hi, avg(n_chars) AS chars FROM meta SEMI JOIN sel USING (id)")).toArray()[0];
        const top = (await conn.query("SELECT token, rank FROM meta SEMI JOIN sel USING (id) ORDER BY rank LIMIT 10")).toArray();
        return {
          rank: agg.lo == null ? null : { min: num(agg.lo), q25: num(agg.q25), median: num(agg.med), q75: num(agg.q75), max: num(agg.hi) },
          meanChars: agg.chars == null ? null : num(agg.chars),
          top: top.map((r) => ({ token: String(r.token), rank: num(r.rank) })),
          ms: performance.now() - t0,
        };
      }),
    lookup: (id) => serial(async () => ((await byId.query(id)).toArray().map((r) => row(r.toJSON()))[0] ?? null)),
    search: (text, limit = 50) => serial(async () => (await byText.query(text, limit)).toArray().map((r) => row(r.toJSON()))),
    close: async () => {
      await conn.close();
      await db.terminate();
    },
  };
}
