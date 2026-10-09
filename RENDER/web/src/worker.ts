/// <reference lib="webworker" />
// Off the main thread: fetch and validate tiles, hold the spatial index, answer lasso and hover
// queries. The main thread keeps only what the GPU needs.

import { PointIndex, type Allow, type Hit } from "./lib/pointIndex";
import { decodeTile, TileError, type Square } from "./lib/tile";

export type Request =
  | { type: "load"; epoch: number; key: string; url: string; z: number; n: number; square: Square }
  | { type: "evict"; keys: string[] }
  | { type: "reset"; epoch: number }
  | { type: "select"; seq: number; polygon: Float64Array; allow: Allow | null; wantIds: boolean }
  | { type: "hit"; seq: number; x: number; y: number; r: number; allow: Allow | null }
  | { type: "find"; seq: number; ids: Uint32Array };

export type Response =
  | { type: "tile"; epoch: number; key: string; n: number; q: Uint16Array; cats: Uint8Array; square: Square; ms: number; bytes: number }
  | { type: "tile-error"; epoch: number; key: string; message: string; attempts: number }
  | { type: "selection"; seq: number; n: number; counts: Uint32Array[]; flags: Map<string, Uint8Array>; ids: Uint32Array | null; ms: number }
  | { type: "hit"; seq: number; hit: Hit | null }
  | { type: "found"; seq: number; points: { id: number; x: number; y: number }[] };

const index = new PointIndex();
let epoch = 0; // bumped when the variant changes, so tiles of the old one are dropped on arrival
const ATTEMPTS = 4;

const post = (msg: Response, transfer: Transferable[] = []) => (self as unknown as DedicatedWorkerGlobalScope).postMessage(msg, transfer);
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Fetch with capped exponential backoff. A 4xx is final; network errors, 5xx and corrupt bodies are retried. */
async function load(req: Extract<Request, { type: "load" }>): Promise<void> {
  const t0 = performance.now();
  let message = "";
  for (let attempt = 1; attempt <= ATTEMPTS; attempt++) {
    if (req.epoch !== epoch) return;
    try {
      const res = await fetch(req.url);
      if (!res.ok) {
        message = `HTTP ${res.status}`;
        if (res.status < 500 && res.status !== 429) return post({ type: "tile-error", epoch: req.epoch, key: req.key, message, attempts: attempt });
      } else {
        const buf = await res.arrayBuffer();
        const tile = decodeTile(buf, req.key, req.z, req.n, req.square);
        if (req.epoch !== epoch) return;
        index.add(tile);
        const q = tile.q.slice(), cats = tile.cats.slice(); // copies for the GPU; the index keeps the originals
        return post({ type: "tile", epoch: req.epoch, key: req.key, n: tile.n, q, cats, square: req.square, ms: performance.now() - t0, bytes: buf.byteLength }, [q.buffer, cats.buffer]);
      }
    } catch (e) {
      message = e instanceof TileError ? e.message : `network error (${(e as Error).message})`;
    }
    if (attempt < ATTEMPTS) await sleep(Math.min(4000, 250 * 2 ** attempt) * (0.5 + Math.random() / 2));
  }
  post({ type: "tile-error", epoch: req.epoch, key: req.key, message, attempts: ATTEMPTS });
}

self.onmessage = (e: MessageEvent<Request>) => {
  const req = e.data;
  switch (req.type) {
    case "load":
      void load(req);
      break;
    case "evict":
      for (const key of req.keys) index.remove(key);
      break;
    case "reset":
      epoch = req.epoch;
      index.clear();
      break;
    case "select": {
      const t0 = performance.now();
      const s = index.select(req.polygon, req.allow, req.wantIds);
      const transfer: Transferable[] = [...s.flags.values()].map((f) => f.buffer);
      if (s.ids) {
        s.ids = s.ids.slice(); // detach from the over-allocated scratch buffer
        transfer.push(s.ids.buffer);
      }
      post({ type: "selection", seq: req.seq, ...s, ms: performance.now() - t0 }, transfer);
      break;
    }
    case "hit":
      post({ type: "hit", seq: req.seq, hit: index.hit(req.x, req.y, req.r, req.allow) });
      break;
    case "find":
      post({ type: "found", seq: req.seq, points: index.find(req.ids) });
      break;
  }
};
