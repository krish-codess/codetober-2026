// The imperative half of the page: owns the canvas, the renderer, the worker, the view and the
// selection. React tells it what the user chose and listens for status; it never re-renders per frame.

import type { Request, Response } from "../worker";
import { bboxOf, simplify } from "./geometry";
import { chooseEvictions, chooseTiles, exposure, pointsInView, tileKey, type Rect } from "./lod";
import type { Allow, Hit } from "./pointIndex";
import { Renderer, type DrawState, type Shading } from "./renderer";
import { paletteTexture, THEMES, type ThemeName } from "./theme";
import { tileSquare } from "./tile";
import type { Manifest, Variant } from "./types";

export type Tool = "pan" | "lasso" | "box";

export interface SceneStatus {
  tilesWanted: number;
  tilesLoaded: number;
  /** tiles the view wants that failed after every retry */
  failed: { key: string; message: string }[];
  pointsDrawn: number;
  /** points inside the view in the full dataset (from the density grid); null until it loads */
  pointsInView: number | null;
  pointsHeld: number;
  bytesLoaded: number;
  fps: number | null;
  shading: Shading;
  gpu: string;
  /** ms from construction to the first frame with points in it */
  firstPaintMs: number | null;
}

export interface SelectionInfo {
  n: number;
  counts: Uint32Array[];
  /** row ids, present once the drag has ended */
  ids: Uint32Array | null;
  /** still dragging */
  live: boolean;
  /** every tile the outline touches is loaded, so n is the whole answer and not a sample */
  exact: boolean;
  /** time inside the worker */
  workerMs: number;
  /** from the pointer event to the answer arriving back on the main thread */
  latencyMs: number;
}

export interface HoverInfo {
  hit: Hit;
  /** CSS pixels inside the canvas */
  sx: number;
  sy: number;
}

export interface SceneOptions {
  canvas: HTMLCanvasElement;
  overlay: HTMLCanvasElement;
  manifest: Manifest;
  /** URL of the data directory, with a trailing slash */
  dataUrl: string;
  variant: string;
  /** most points drawn at once; also bounds memory (1.25x is held) */
  budget: number;
  theme: ThemeName;
  onStatus: (s: SceneStatus) => void;
  onSelection: (s: SelectionInfo | null) => void;
  onHover: (h: HoverInfo | null) => void;
  onError: (message: string) => void;
  createWorker?: () => Worker;
}

export interface BenchResult {
  seconds: number;
  frames: number;
  fps: number;
  frameMs: { p50: number; p95: number; p99: number; max: number };
  /** share of frames that took longer than 1.5 refresh intervals at 60 Hz (25 ms) */
  dropped: number;
  /** ms per frame with the GPU forced to finish, not capped by vsync */
  uncappedFrameMs: number;
  pointsDrawn: { min: number; max: number };
  canvas: [number, number];
  dpr: number;
  gpu: string;
  shading: Shading;
}

const MAX_DPR = 2;

export class Scene {
  private o: SceneOptions;
  private renderer: Renderer;
  private worker: Worker;
  private ctx: CanvasRenderingContext2D | null;
  private variant!: Variant;
  private epoch = 0;
  private born = performance.now();

  // view: world coordinates at the canvas centre, CSS pixels per world unit
  private cx = 0;
  private cy = 0;
  private scale = 1;
  private fitScale = 1;
  private w = 1;
  private h = 1;
  private dpr = 1;
  private moved = false;

  private loaded = new Map<string, { n: number; lastWanted: number }>();
  private loading = new Set<string>();
  private failed = new Map<string, string>();
  private drawKeys: string[] = [];
  private wantedCount = 0;
  private bytes = 0;
  private grid: Uint32Array | null = null;

  private column = 0;
  private allow: Allow | null = null;
  private theme: ThemeName;
  private tool: Tool = "pan";

  private polygon: number[] | null = null; // world coordinates
  private dragging: "pan" | "select" | null = null;
  private anchor = { x: 0, y: 0 };
  private pointers = new Map<number, { x: number; y: number }>();
  private pinch = 0;
  private seq = 0;
  private inFlight: { seq: number; sentAt: number; final: boolean } | null = null;
  private pending: { final: boolean; at: number } | null = null;
  private selectionActive = false;
  private lastSelection: SelectionInfo | null = null;
  private hoverBusy = false;
  private hover: HoverInfo | null = null;
  private markers: { id: number; x: number; y: number }[] = [];

  private dirty = true;
  private raf = 0;
  private frames: number[] = [];
  private lastStatus = 0;
  private firstPaintMs: number | null = null;
  private pointsDrawn = 0;
  private stop: (() => void)[] = [];

  constructor(o: SceneOptions) {
    this.o = o;
    this.theme = o.theme;
    this.renderer = new Renderer(o.canvas);
    this.ctx = o.overlay.getContext("2d");
    this.worker = (o.createWorker ?? (() => new Worker(new URL("../worker.ts", import.meta.url), { type: "module" })))();
    this.worker.onmessage = (e: MessageEvent<Response>) => this.onWorker(e.data);
    this.worker.onerror = (e) => o.onError(`The data worker stopped: ${e.message}`);
    this.listen();
    this.resize();
    this.setVariant(o.variant);
    this.loop();
  }

  // --- configuration ------------------------------------------------------------------------

  setVariant(name: string): void {
    const v = this.o.manifest.variants.find((x) => x.name === name);
    if (!v) return this.o.onError(`Unknown projection ${name}.`);
    this.variant = v;
    this.epoch++;
    this.send({ type: "reset", epoch: this.epoch });
    this.renderer.dropAll();
    this.loaded.clear();
    this.loading.clear();
    this.failed.clear();
    this.grid = null;
    this.markers = [];
    this.clearSelection();
    this.resetView();
    const epoch = this.epoch;
    // The density grid is an optimisation (exposure and "n in view"); the page works without it.
    fetch(`${this.o.dataUrl}${v.tiles_path}/density.u32`)
      .then((r) => (r.ok ? r.arrayBuffer() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((buf) => {
        if (epoch === this.epoch && buf.byteLength === v.density_grid ** 2 * 4) this.grid = new Uint32Array(buf);
        this.dirty = true;
      })
      .catch(() => undefined);
  }

  setColumn(column: number): void {
    this.column = column;
    this.dirty = true;
  }

  setFilter(allow: Allow | null): void {
    this.allow = allow;
    this.renderer.setPalette(paletteTexture(this.theme, allow));
    if (this.polygon) this.requestSelect(true);
    this.dirty = true;
  }

  setTheme(theme: ThemeName): void {
    this.theme = theme;
    this.renderer.setPalette(paletteTexture(theme, this.allow));
    this.dirty = true;
  }

  setTool(tool: Tool): void {
    this.tool = tool;
  }

  // --- view ---------------------------------------------------------------------------------

  resetView(): void {
    const [x0, y0, x1, y1] = this.variant.view;
    this.cx = (x0 + x1) / 2;
    this.cy = (y0 + y1) / 2;
    this.fitScale = this.scale = 0.92 * Math.min(this.w / Math.max(x1 - x0, 1e-12), this.h / Math.max(y1 - y0, 1e-12));
    this.moved = false;
    this.dirty = true;
  }

  /** Pan by CSS pixels. */
  pan(dx: number, dy: number): void {
    this.cx -= dx / this.scale;
    this.cy += dy / this.scale;
    this.moved = this.dirty = true;
  }

  /** Zoom by `factor` keeping the point under (sx, sy) fixed; the centre when omitted. */
  zoom(factor: number, sx = this.w / 2, sy = this.h / 2): void {
    const [wx, wy] = this.toWorld(sx, sy);
    this.scale = Math.min(this.fitScale * 2 ** 14, Math.max(this.fitScale * 0.25, this.scale * factor));
    this.cx = wx - (sx - this.w / 2) / this.scale;
    this.cy = wy + (sy - this.h / 2) / this.scale;
    this.moved = this.dirty = true;
  }

  flyTo(x: number, y: number): void {
    this.cx = x;
    this.cy = y;
    this.scale = Math.max(this.scale, this.fitScale * 16);
    this.moved = this.dirty = true;
  }

  private toWorld(sx: number, sy: number): [number, number] {
    return [this.cx + (sx - this.w / 2) / this.scale, this.cy - (sy - this.h / 2) / this.scale];
  }

  private toScreen(x: number, y: number): [number, number] {
    return [(x - this.cx) * this.scale + this.w / 2, this.h / 2 - (y - this.cy) * this.scale];
  }

  private viewRect(): Rect {
    const hw = this.w / 2 / this.scale, hh = this.h / 2 / this.scale;
    return { x0: this.cx - hw, y0: this.cy - hh, x1: this.cx + hw, y1: this.cy + hh };
  }

  private resize(): void {
    const { canvas, overlay } = this.o;
    const rect = canvas.getBoundingClientRect();
    this.w = Math.max(1, rect.width);
    this.h = Math.max(1, rect.height);
    this.dpr = Math.min(window.devicePixelRatio || 1, MAX_DPR);
    for (const c of [canvas, overlay]) {
      c.width = Math.round(this.w * this.dpr);
      c.height = Math.round(this.h * this.dpr);
    }
    if (this.variant && !this.moved) this.resetView(); // until the user moves, keep the data fitted to the canvas
    this.dirty = true;
  }

  // --- selection ----------------------------------------------------------------------------

  clearSelection(): void {
    this.polygon = null;
    this.pending = null;
    this.selectionActive = false;
    this.seq++; // an answer still on its way back is now stale
    this.inFlight = null;
    this.renderer.clearFlags();
    this.lastSelection = null;
    this.o.onSelection(null);
    this.dirty = true;
  }

  /** Select everything in the current view: the keyboard's way to make a box selection. */
  selectView(): void {
    const r = this.viewRect();
    this.selectPolygon([r.x0, r.y0, r.x1, r.y0, r.x1, r.y1, r.x0, r.y1]);
  }

  /** Select with an outline given in projection units, flat [x0, y0, x1, y1, ...]. */
  selectPolygon(polygon: number[]): void {
    this.clearSelection();
    this.polygon = polygon;
    this.requestSelect(true);
  }

  /** What the page currently knows, for the benchmark and the end-to-end test. */
  debug(): { status: SceneStatus; selection: Omit<SelectionInfo, "counts" | "ids"> | null; view: Rect } {
    const s = this.lastSelection;
    return { status: this.status(), selection: s && { n: s.n, live: s.live, exact: s.exact, workerMs: s.workerMs, latencyMs: s.latencyMs }, view: this.viewRect() };
  }

  /** At most one query is in the worker at a time; a newer outline replaces any that is waiting. */
  private requestSelect(final: boolean): void {
    if (!this.polygon || this.polygon.length < 6) return;
    if (this.inFlight) {
      this.pending = { final: final || !!this.pending?.final, at: this.pending?.at ?? performance.now() };
      return;
    }
    const sentAt = this.pending?.at ?? performance.now();
    this.pending = null;
    const polygon = Float64Array.from(simplify(this.polygon, 1.5 / this.scale));
    this.inFlight = { seq: ++this.seq, sentAt, final };
    this.send({ type: "select", seq: this.seq, polygon, allow: this.allow, wantIds: final });
  }

  private selectionIsExact(): boolean {
    if (!this.polygon) return true;
    const [x0, y0, x1, y1] = bboxOf(this.polygon);
    for (const t of this.variant.tiles) {
      const sq = tileSquare(this.variant.bounds, t[0], t[1], t[2]);
      if (sq.x <= x1 && sq.x + sq.side >= x0 && sq.y <= y1 && sq.y + sq.side >= y0 && !this.loaded.has(tileKey(t))) return false;
    }
    return true;
  }

  showMarkers(ids: Uint32Array): void {
    this.markers = [];
    this.dirty = true;
    if (ids.length) this.send({ type: "find", seq: ++this.seq, ids });
  }

  // --- worker -------------------------------------------------------------------------------

  private send(msg: Request): void {
    this.worker.postMessage(msg);
  }

  private onWorker(msg: Response): void {
    switch (msg.type) {
      case "tile":
        if (msg.epoch !== this.epoch) return;
        this.loading.delete(msg.key);
        this.loaded.set(msg.key, { n: msg.n, lastWanted: performance.now() });
        this.renderer.setTile(msg.key, msg.n, msg.q, msg.cats, msg.square);
        this.bytes += msg.bytes;
        if (this.polygon && !this.dragging) this.requestSelect(true); // the selection may now include this tile's points
        break;
      case "tile-error":
        if (msg.epoch !== this.epoch) return;
        this.loading.delete(msg.key);
        this.failed.set(msg.key, msg.message);
        break;
      case "selection": {
        const flight = this.inFlight;
        if (!flight || msg.seq !== flight.seq) return; // cleared or superseded
        this.inFlight = null;
        for (const [key, flags] of msg.flags) this.renderer.setFlags(key, flags);
        this.selectionActive = true;
        this.lastSelection = { n: msg.n, counts: msg.counts, ids: msg.ids, live: !flight.final, exact: this.selectionIsExact(), workerMs: msg.ms, latencyMs: performance.now() - flight.sentAt };
        this.o.onSelection(this.lastSelection);
        if (this.pending) this.requestSelect(this.pending.final);
        break;
      }
      case "hit": {
        this.hoverBusy = false;
        const hit = msg.hit;
        if (hit && !this.dragging) {
          const [sx, sy] = this.toScreen(hit.x, hit.y);
          this.hover = { hit, sx, sy };
        } else this.hover = null;
        this.o.onHover(this.hover);
        break;
      }
      case "found":
        this.markers = msg.points;
        break;
    }
    this.dirty = true;
  }

  retryFailed(): void {
    this.failed.clear();
    this.dirty = true;
  }

  private updateTiles(): void {
    const now = performance.now();
    const v = this.viewRect();
    const margin = 0.1 * (v.x1 - v.x0);
    const wanted = chooseTiles(this.variant, { x0: v.x0 - margin, y0: v.y0 - margin, x1: v.x1 + margin, y1: v.y1 + margin }, this.o.budget);
    const keys = new Set<string>();
    this.drawKeys = [];
    for (const t of wanted) {
      const key = tileKey(t);
      keys.add(key);
      const have = this.loaded.get(key);
      if (have) {
        have.lastWanted = now;
        this.drawKeys.push(key);
      } else if (!this.loading.has(key) && !this.failed.has(key)) {
        this.loading.add(key);
        this.send({ type: "load", epoch: this.epoch, key, url: `${this.o.dataUrl}${this.variant.tiles_path}/${key}.bin`, z: t[0], n: t[3], square: tileSquare(this.variant.bounds, t[0], t[1], t[2]) });
      }
    }
    this.wantedCount = wanted.length;
    const drop = chooseEvictions(this.loaded, keys, this.o.budget * 1.25);
    if (drop.length) {
      for (const key of drop) {
        this.loaded.delete(key);
        this.renderer.dropTile(key);
      }
      this.send({ type: "evict", keys: drop });
    }
  }

  // --- drawing ------------------------------------------------------------------------------

  private drawState(): DrawState {
    // Sprite radius in device pixels. Zoomed out there are more points than pixels, so a point is
    // one pixel and the picture is a density map; it grows into a disc as zooming in makes room.
    const radius = Math.min(5 * this.dpr, Math.max(0.5, 0.35 * this.dpr * (this.scale / this.fitScale) ** 0.45));
    const view = this.viewRect();
    let reference = 8;
    if (this.grid) {
      const inView = pointsInView(this.grid, this.variant.density_grid, this.variant.bounds, view);
      const share = inView > 0 ? Math.min(1, this.pointsDrawn / inView) : 1;
      reference = exposure(this.grid, this.variant.density_grid, this.variant.bounds, view, this.scale * this.dpr, radius, share);
    }
    return { cx: this.cx, cy: this.cy, scale: this.scale * this.dpr, radius, column: this.column, selectionActive: this.selectionActive, reference, theme: THEMES[this.theme], keys: this.drawKeys };
  }

  private frame(): void {
    this.updateTiles();
    this.pointsDrawn = this.renderer.draw(this.drawState());
    if (this.firstPaintMs === null && this.pointsDrawn > 0) this.firstPaintMs = performance.now() - this.born;
    this.drawOverlay();
  }

  private drawOverlay(): void {
    const c = this.ctx;
    if (!c) return;
    c.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    c.clearRect(0, 0, this.w, this.h);
    const ink = this.theme === "dark" ? "#ffffff" : "#0b0b0b";
    if (this.polygon && this.polygon.length >= 4) {
      c.beginPath();
      for (let i = 0; i < this.polygon.length; i += 2) {
        const [sx, sy] = this.toScreen(this.polygon[i], this.polygon[i + 1]);
        if (i) c.lineTo(sx, sy);
        else c.moveTo(sx, sy);
      }
      c.closePath();
      c.lineWidth = 1.5;
      c.strokeStyle = ink;
      c.setLineDash(this.dragging === "select" ? [5, 4] : []); // dashed while it is still being drawn
      c.stroke();
      c.setLineDash([]);
    }
    for (const m of this.markers) {
      const [sx, sy] = this.toScreen(m.x, m.y);
      c.beginPath();
      c.arc(sx, sy, 6, 0, 2 * Math.PI);
      c.lineWidth = 2;
      c.strokeStyle = ink;
      c.stroke();
    }
    if (this.hover) {
      const [sx, sy] = this.toScreen(this.hover.hit.x, this.hover.hit.y);
      c.beginPath();
      c.arc(sx, sy, 5, 0, 2 * Math.PI);
      c.lineWidth = 2;
      c.strokeStyle = ink;
      c.stroke();
    }
  }

  private status(): SceneStatus {
    const span = this.frames.length > 1 ? this.frames[this.frames.length - 1] - this.frames[0] : 0;
    let held = 0;
    for (const t of this.loaded.values()) held += t.n;
    return {
      tilesWanted: this.wantedCount,
      tilesLoaded: this.drawKeys.length,
      failed: [...this.failed].map(([key, message]) => ({ key, message })),
      pointsDrawn: this.pointsDrawn,
      pointsInView: this.grid ? pointsInView(this.grid, this.variant.density_grid, this.variant.bounds, this.viewRect()) : null,
      pointsHeld: held,
      bytesLoaded: this.bytes,
      fps: span > 0 ? ((this.frames.length - 1) * 1000) / span : null,
      shading: this.renderer.shading,
      gpu: this.renderer.gpu,
      firstPaintMs: this.firstPaintMs,
    };
  }

  private loop = (): void => {
    this.raf = requestAnimationFrame(this.loop);
    const now = performance.now();
    if (this.dirty) {
      this.dirty = false;
      this.frame();
      this.frames.push(now);
      while (this.frames.length > 1 && now - this.frames[0] > 1000) this.frames.shift();
    } else if (this.frames.length && now - this.frames[this.frames.length - 1] > 300) this.frames = []; // idle: an fps figure would be meaningless
    if (now - this.lastStatus > 200) {
      this.lastStatus = now;
      this.o.onStatus(this.status());
    }
  };

  /** Pan and zoom continuously for `ms`, recording every frame interval. Used by the committed benchmark. */
  async bench(ms = 6000): Promise<BenchResult> {
    const base = { cx: this.cx, cy: this.cy, scale: this.scale };
    const deltas: number[] = [];
    let min = Infinity, max = 0;
    await new Promise<void>((done) => {
      const t0 = performance.now();
      let last = t0;
      const tick = (t: number): void => {
        const p = (t - t0) / ms;
        if (p >= 1) return done();
        // Circle the view and breathe the zoom, so no two frames draw the same thing.
        this.cx = base.cx + (Math.cos(p * 4 * Math.PI) * 0.1 * this.w) / base.scale;
        this.cy = base.cy + (Math.sin(p * 4 * Math.PI) * 0.1 * this.h) / base.scale;
        this.scale = base.scale * (1 + 0.3 * Math.sin(p * 6 * Math.PI));
        this.dirty = true;
        if (t > last) deltas.push(t - last);
        last = t;
        min = Math.min(min, this.pointsDrawn);
        max = Math.max(max, this.pointsDrawn);
        requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    });
    Object.assign(this, base);
    this.frame();
    const uncapped = this.renderer.timeDraw(this.drawState(), 30);
    deltas.sort((a, b) => a - b);
    const q = (p: number): number => deltas[Math.min(deltas.length - 1, Math.floor(p * deltas.length))];
    const total = deltas.reduce((a, b) => a + b, 0);
    return {
      seconds: total / 1000,
      frames: deltas.length,
      fps: (deltas.length * 1000) / total,
      frameMs: { p50: q(0.5), p95: q(0.95), p99: q(0.99), max: deltas[deltas.length - 1] },
      dropped: deltas.filter((d) => d > 25).length / deltas.length,
      uncappedFrameMs: uncapped,
      pointsDrawn: { min, max },
      canvas: [this.o.canvas.width, this.o.canvas.height],
      dpr: this.dpr,
      gpu: this.renderer.gpu,
      shading: this.renderer.shading,
    };
  }

  // --- input --------------------------------------------------------------------------------

  private listen(): void {
    const el = this.o.canvas;
    const on = <K extends keyof HTMLElementEventMap>(type: K, fn: (e: HTMLElementEventMap[K]) => void, opts?: AddEventListenerOptions): void => {
      el.addEventListener(type, fn, opts);
      this.stop.push(() => el.removeEventListener(type, fn, opts));
    };
    const at = (e: PointerEvent | WheelEvent): [number, number] => {
      const r = el.getBoundingClientRect();
      return [e.clientX - r.left, e.clientY - r.top];
    };

    on("pointerdown", (e) => {
      el.setPointerCapture(e.pointerId);
      const [x, y] = at(e);
      this.pointers.set(e.pointerId, { x, y });
      this.hover = null;
      this.o.onHover(null);
      if (this.pointers.size === 2) {
        // A second finger always means pinch-zoom, whatever the tool: drop any outline in progress.
        if (this.dragging === "select") this.clearSelection();
        this.dragging = "pan";
        const [a, b] = [...this.pointers.values()];
        this.pinch = Math.hypot(a.x - b.x, a.y - b.y);
      } else if (this.tool === "pan" || e.button !== 0) {
        this.dragging = "pan";
      } else {
        this.dragging = "select";
        this.clearSelection();
        this.anchor = { x, y };
        this.polygon = [...this.toWorld(x, y)];
      }
    });

    on("pointermove", (e) => {
      const [x, y] = at(e);
      const prev = this.pointers.get(e.pointerId);
      if (!prev) {
        if (e.pointerType === "mouse" && !this.hoverBusy) {
          this.hoverBusy = true;
          const [wx, wy] = this.toWorld(x, y);
          this.send({ type: "hit", seq: 0, x: wx, y: wy, r: 8 / this.scale, allow: this.allow });
        }
        return;
      }
      this.pointers.set(e.pointerId, { x, y });
      if (this.pointers.size === 2) {
        const [a, b] = [...this.pointers.values()];
        const d = Math.hypot(a.x - b.x, a.y - b.y);
        if (this.pinch > 0 && d > 0) this.zoom(d / this.pinch, (a.x + b.x) / 2, (a.y + b.y) / 2);
        this.pinch = d;
        this.pan((x - prev.x) / 2, (y - prev.y) / 2);
      } else if (this.dragging === "pan") {
        this.pan(x - prev.x, y - prev.y);
      } else if (this.dragging === "select" && this.polygon) {
        if (this.tool === "box") {
          const [x0, y0] = this.toWorld(this.anchor.x, this.anchor.y), [x1, y1] = this.toWorld(x, y);
          this.polygon = [x0, y0, x1, y0, x1, y1, x0, y1];
        } else this.polygon.push(...this.toWorld(x, y));
        this.requestSelect(false); // statistics while the button is still down
        this.dirty = true;
      }
    });

    const end = (e: PointerEvent): void => {
      if (!this.pointers.delete(e.pointerId)) return;
      if (this.pointers.size) return;
      const was = this.dragging;
      this.dragging = null;
      this.pinch = 0;
      if (was === "select") {
        const [x, y] = at(e);
        if (!this.polygon || this.polygon.length < 6 || Math.hypot(x - this.anchor.x, y - this.anchor.y) < 3 && this.polygon.length < 12) this.clearSelection(); // a click, not a drag
        else this.requestSelect(true);
      }
      this.dirty = true;
    };
    on("pointerup", end);
    on("pointercancel", end);
    on("pointerleave", () => {
      if (this.hover) {
        this.hover = null;
        this.o.onHover(null);
        this.dirty = true;
      }
    });
    on(
      "wheel",
      (e) => {
        e.preventDefault();
        const [x, y] = at(e);
        this.zoom(Math.exp(-e.deltaY * (e.deltaMode === 1 ? 0.05 : 0.0015)), x, y);
      },
      { passive: false },
    );
    on("contextmenu", (e) => e.preventDefault());
    on("webglcontextlost" as keyof HTMLElementEventMap, (e) => {
      e.preventDefault();
      cancelAnimationFrame(this.raf);
      this.o.onError("The browser took the GPU away from this page (WebGL context lost). Reload to continue.");
    });
    const ro = new ResizeObserver(() => this.resize());
    ro.observe(el);
    this.stop.push(() => ro.disconnect());
  }

  destroy(): void {
    cancelAnimationFrame(this.raf);
    for (const fn of this.stop) fn();
    this.worker.terminate();
    this.renderer.destroy();
  }
}

export type SceneLike = Pick<Scene, "setVariant" | "setColumn" | "setFilter" | "setTheme" | "setTool" | "resetView" | "pan" | "zoom" | "flyTo" | "clearSelection" | "selectView" | "selectPolygon" | "debug" | "showMarkers" | "retryFailed" | "destroy" | "bench">;
