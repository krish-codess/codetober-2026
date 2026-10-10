// regl point renderer. Two passes:
//
//  1. Accumulate. Every point is one instance of a triangle, drawn additively into a
//     floating-point target as (colour x weight, weight). Zoomed out, a point is exactly one
//     pixel; zoomed in, a disc with a smooth falloff. The target ends up holding, per pixel, how
//     much "point" landed there and the weighted sum of its colours.
//  2. Resolve. Density = log(1 + weight) / log(1 + reference), which is what makes a region with
//     1,000 points per pixel distinguishable from one with 10 (alpha blending saturates at a
//     handful). Hue is the weighted mean colour, so mixed regions read as mixed.

import createREGL, { type Buffer, type Regl } from "regl";
import type { Square } from "./tile";

export type RGB = [number, number, number];

export interface Theme {
  bg: RGB;
  muted: RGB;
  /** five stops, sparse to dense */
  ramp: RGB[];
}

export interface DrawState {
  /** world coordinates at the centre of the canvas */
  cx: number;
  cy: number;
  /** device pixels per world unit */
  scale: number;
  /** sprite radius in device pixels */
  radius: number;
  /** 0..2 = colour by that category column, -1 = density only */
  column: number;
  selectionActive: boolean;
  /** accumulated weight that maps to the top of the ramp */
  reference: number;
  theme: Theme;
  keys: Iterable<string>;
}

interface GpuTile {
  n: number;
  square: Square;
  xy: Buffer;
  cats: Buffer;
  flags: Buffer | null;
}

export type Shading = "float" | "half float" | "8-bit (degraded)";

const VERT = `
precision highp float;
attribute vec2 corner;
attribute vec2 xy;
attribute vec4 cat;
attribute float sel;
uniform vec2 uOrigin, uInv, uHalf;
uniform float uSide, uRadius, uColumn, uSelActive, uSelFixed;
uniform sampler2D uPalette;
uniform vec3 uMuted, uFiltered;
varying vec2 vCorner;
varying vec3 vColor;
varying float vWeight;
// cat arrives normalised (code / 255): a native vertex format everywhere, where raw bytes read as floats are not.
vec4 look(float code, float row) { return texture2D(uPalette, vec2((code * 255.0 + 0.5) / 256.0, (row + 0.5) / 4.0)); }
void main() {
  // Texture reads in a vertex shader are the expensive part of a point, so only the ones this
  // frame needs happen: one for the colour column, one per column that has a filter on it.
  vec3 color = vec3(1.0);
  if (uColumn > -0.5) color = look(uColumn < 0.5 ? cat.x : (uColumn < 1.5 ? cat.y : cat.z), uColumn).rgb;
  float shown = 1.0;
  if (uFiltered.x > 0.5) shown *= look(cat.x, 0.0).a;
  if (uFiltered.y > 0.5) shown *= look(cat.y, 1.0).a;
  if (uFiltered.z > 0.5) shown *= look(cat.z, 2.0).a;
  float w = 1.0;
  if (uSelActive > 0.5 && (uSelFixed < -0.5 ? sel : uSelFixed) < 0.5) {
    // Density mode carries "share selected" in the red channel; colour mode greys the rest out.
    if (uColumn < -0.5) color = vec3(0.0); else { color = uMuted; w = 0.3; }
  }
  vColor = color;
  vWeight = w;
  vCorner = corner;
  vec2 px = uOrigin + xy * uSide;
  if (uRadius > 0.75) {
    px += corner * uRadius; // a triangle around the disc; the fragment shader trims it
  } else {
    // A one-pixel point: snap to the pixel it falls in and draw a triangle small enough to cover
    // that pixel's centre and no other. One triangle and exactly one fragment per point.
    px = floor(px + uHalf) + 0.5 - uHalf + corner * 0.3;
  }
  // The alpha of each palette row is the category filter: filtered-out points go off screen.
  gl_Position = shown > 0.5 ? vec4(px * uInv, 0.0, 1.0) : vec4(2.0, 2.0, 2.0, 1.0);
}`;

const FRAG = `
precision highp float;
uniform float uWeightScale, uRadius;
varying vec2 vCorner;
varying vec3 vColor;
varying float vWeight;
void main() {
  float w = vWeight * uWeightScale;
  // A sprite about one pixel across is a single fragment: it counts as one whole point.
  // Larger sprites become discs with a smooth falloff, so neighbours blend instead of tiling.
  if (uRadius > 0.75) {
    float d = dot(vCorner, vCorner);
    if (d > 1.0) discard;
    w *= exp(-2.5 * d * min(1.0, uRadius - 0.75));
  }
  gl_FragColor = vec4(vColor * w, w);
}`;

const RESOLVE = `
precision highp float;
uniform sampler2D uAcc;
uniform vec2 uPixel;
uniform float uReference, uColumn, uWeightScale;
uniform vec3 uBg, uMuted;
uniform vec3 uRamp[5];
vec3 ramp(float t) {
  float s = t * 4.0;
  float i = floor(min(s, 3.0));
  vec3 lo = i < 0.5 ? uRamp[0] : (i < 1.5 ? uRamp[1] : (i < 2.5 ? uRamp[2] : uRamp[3]));
  vec3 hi = i < 0.5 ? uRamp[1] : (i < 1.5 ? uRamp[2] : (i < 2.5 ? uRamp[3] : uRamp[4]));
  return mix(lo, hi, s - i);
}
void main() {
  vec4 acc = texture2D(uAcc, gl_FragCoord.xy * uPixel) / uWeightScale;
  if (acc.a < 1e-4) { gl_FragColor = vec4(uBg, 1.0); return; }
  float t = clamp(log(1.0 + acc.a) / log(1.0 + uReference), 0.0, 1.0);
  vec3 mean = acc.rgb / acc.a;
  vec3 col = uColumn < -0.5
    ? mix(mix(uBg, uMuted, 0.3 + 0.5 * t), ramp(t), mean.r)
    : mix(uBg, mean, 0.35 + 0.65 * t);
  gl_FragColor = vec4(mix(uBg, col, clamp(acc.a * 4.0, 0.0, 1.0)), 1.0);
}`;

export class Renderer {
  readonly shading: Shading;
  readonly gpu: string;
  private regl: Regl;
  private tiles = new Map<string, GpuTile>();
  private fbo: createREGL.Framebuffer2D;
  private palette: createREGL.Texture2D;
  private weightScale: number;
  private fboW = 4;
  private filtered = [0, 0, 0];
  private fboH = 4;
  private drawPoints: createREGL.DrawCommand;
  private resolve: createREGL.DrawCommand;

  constructor(private canvas: HTMLCanvasElement) {
    const regl = (this.regl = createREGL({
      canvas,
      attributes: { alpha: false, antialias: false, depth: false, stencil: false, powerPreference: "high-performance" },
      extensions: ["ANGLE_instanced_arrays"],
      optionalExtensions: ["OES_texture_float", "WEBGL_color_buffer_float", "OES_texture_half_float", "EXT_color_buffer_half_float", "WEBGL_debug_renderer_info"],
    }));
    if (regl.limits.maxVertexTextureUnits < 1) throw new Error("This GPU cannot read textures in a vertex shader, which the category filter needs.");
    const gl = regl._gl;
    const info = gl.getExtension("WEBGL_debug_renderer_info");
    this.gpu = String(info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER));

    // Best target the GPU can render to; without any float target, fall back to scaled 8-bit and say so.
    const candidates: [Shading, "float" | "half float" | "uint8", boolean][] = [
      ["float", "float", regl.hasExtension("OES_texture_float") && regl.hasExtension("WEBGL_color_buffer_float")],
      ["half float", "half float", regl.hasExtension("OES_texture_half_float") && regl.hasExtension("EXT_color_buffer_half_float")],
      ["8-bit (degraded)", "uint8", true],
    ];
    let made: [Shading, createREGL.Framebuffer2D] | null = null;
    for (const [name, type, supported] of candidates) {
      if (!supported) continue;
      try {
        made = [name, regl.framebuffer({ color: regl.texture({ width: 4, height: 4, type, format: "rgba", min: "nearest", mag: "nearest" }), depth: false, stencil: false })];
        break;
      } catch {
        // the extension is advertised but the framebuffer is not renderable: try the next format
      }
    }
    if (!made) throw new Error("WebGL could not create a render target.");
    [this.shading, this.fbo] = made;
    this.weightScale = this.shading === "8-bit (degraded)" ? 1 / 16 : 1;
    this.palette = regl.texture({ width: 256, height: 4, format: "rgba", type: "uint8", min: "nearest", mag: "nearest" });

    this.drawPoints = regl({
      vert: VERT,
      frag: FRAG,
      attributes: {
        corner: [-1, -1, 3, -1, -1, 3],
        xy: regl.prop<any, "xy">("xy"),
        cat: regl.prop<any, "cat">("cat"),
        sel: regl.prop<any, "sel">("sel"),
      },
      uniforms: {
        uOrigin: regl.prop<any, "origin">("origin"),
        uSide: regl.prop<any, "side">("side"),
        uInv: regl.prop<any, "inv">("inv"),
        uHalf: regl.prop<any, "half">("half"),
        uRadius: regl.prop<any, "radius">("radius"),
        uColumn: regl.prop<any, "column">("column"),
        uSelActive: regl.prop<any, "selActive">("selActive"),
        uSelFixed: regl.prop<any, "selFixed">("selFixed"),
        uMuted: regl.prop<any, "muted">("muted"),
        uPalette: this.palette,
        uFiltered: () => this.filtered,
        uWeightScale: this.weightScale,
      },
      primitive: "triangles",
      count: 3,
      instances: regl.prop<any, "n">("n"),
      depth: { enable: false },
      blend: { enable: true, func: { src: "one", dst: "one" } },
    });

    this.resolve = regl({
      vert: "precision highp float; attribute vec2 p; void main() { gl_Position = vec4(p, 0.0, 1.0); }",
      frag: RESOLVE,
      attributes: { p: [-1, -1, 3, -1, -1, 3] },
      uniforms: {
        uAcc: this.fbo,
        uPixel: regl.prop<any, "pixel">("pixel"),
        uReference: regl.prop<any, "reference">("reference"),
        uColumn: regl.prop<any, "column">("column"),
        uBg: regl.prop<any, "bg">("bg"),
        uMuted: regl.prop<any, "muted">("muted"),
        "uRamp[0]": regl.prop<any, "r0">("r0"),
        "uRamp[1]": regl.prop<any, "r1">("r1"),
        "uRamp[2]": regl.prop<any, "r2">("r2"),
        "uRamp[3]": regl.prop<any, "r3">("r3"),
        "uRamp[4]": regl.prop<any, "r4">("r4"),
        uWeightScale: this.weightScale,
      },
      count: 3,
      depth: { enable: false },
    });
  }

  /** rgba per (column row, category code): colour and, in alpha, whether the category passes the filter. */
  setPalette(data: Uint8Array): void {
    // Which columns hide anything at all: the shader skips the lookup for the others.
    this.filtered = [0, 1, 2].map((row) => {
      for (let code = 0; code < 256; code++) if (data[(row * 256 + code) * 4 + 3] === 0) return 1;
      return 0;
    });
    this.palette({ width: 256, height: 4, format: "rgba", type: "uint8", data, min: "nearest", mag: "nearest" });
  }

  setTile(key: string, n: number, q: Uint16Array, cats: Uint8Array, square: Square): void {
    this.dropTile(key);
    this.tiles.set(key, { n, square, xy: this.regl.buffer({ data: q, type: "uint16" }), cats: this.regl.buffer({ data: cats, type: "uint8" }), flags: null });
  }

  setFlags(key: string, flags: Uint8Array | null): void {
    const t = this.tiles.get(key);
    if (!t) return;
    if (!flags) {
      t.flags?.destroy();
      t.flags = null;
    } else if (t.flags) t.flags.subdata(flags);
    else t.flags = this.regl.buffer({ data: flags, type: "uint8", usage: "dynamic" });
  }

  clearFlags(): void {
    for (const key of this.tiles.keys()) this.setFlags(key, null);
  }

  dropTile(key: string): void {
    const t = this.tiles.get(key);
    if (!t) return;
    t.xy.destroy();
    t.cats.destroy();
    t.flags?.destroy();
    this.tiles.delete(key);
  }

  dropAll(): void {
    for (const key of [...this.tiles.keys()]) this.dropTile(key);
  }

  has(key: string): boolean {
    return this.tiles.has(key);
  }

  /** Draw one frame. Returns the number of points submitted. */
  draw(s: DrawState): number {
    const { canvas, regl } = this;
    regl.poll();
    const w = canvas.width, h = canvas.height;
    if (this.fboW !== w || this.fboH !== h) this.fbo.resize((this.fboW = w), (this.fboH = h));
    const batch: Record<string, unknown>[] = [];
    let points = 0;
    for (const key of s.keys) {
      const t = this.tiles.get(key);
      if (!t || !t.n) continue;
      points += t.n;
      batch.push({
        n: t.n,
        // Subtract the view centre in float64 here, so the shader's float32 only ever holds on-screen-sized numbers.
        origin: [(t.square.x - s.cx) * s.scale, (t.square.y - s.cy) * s.scale],
        side: t.square.side * s.scale,
        inv: [2 / w, 2 / h],
        half: [w / 2, h / 2],
        radius: s.radius,
        column: s.column,
        selActive: s.selectionActive ? 1 : 0,
        muted: s.theme.muted,
        xy: { buffer: t.xy, size: 2, normalized: true, divisor: 1 },
        cat: { buffer: t.cats, size: 4, normalized: true, divisor: 1 },
        // No flags yet for a tile that arrived mid-selection: draw it unselected until the worker answers.
        sel: { buffer: t.flags ?? t.cats, size: 1, normalized: true, divisor: 1 },
        selFixed: t.flags ? -1 : s.selectionActive ? 0 : 1,
      });
    }
    this.fbo.use(() => {
      regl.clear({ color: [0, 0, 0, 0] });
      if (batch.length) this.drawPoints(batch);
    });
    const r = s.theme.ramp;
    this.resolve({ pixel: [1 / w, 1 / h], reference: s.reference, column: s.column, bg: s.theme.bg, muted: s.theme.muted, r0: r[0], r1: r[1], r2: r[2], r3: r[3], r4: r[4] });
    return points;
  }

  /** Milliseconds per frame with the GPU forced to finish each one: the real cost, not capped by vsync. */
  timeDraw(s: DrawState, frames: number): number {
    const px = new Uint8Array(4);
    const gl = this.regl._gl;
    this.draw(s);
    gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px);
    const t0 = performance.now();
    for (let i = 0; i < frames; i++) {
      this.draw(s);
      gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px); // blocks until the frame is actually rendered
    }
    return (performance.now() - t0) / frames;
  }

  destroy(): void {
    this.regl.destroy();
  }
}
