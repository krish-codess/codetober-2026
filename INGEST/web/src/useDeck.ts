import { Deck, MapView, type Layer, type MapViewState, type PickingInfo } from "@deck.gl/core";
import { GeoJsonLayer } from "@deck.gl/layers";
import { useEffect, useRef, type RefObject } from "react";

export interface Palette {
  land: [number, number, number];
  coast: [number, number, number];
  moving: [number, number, number];
  still: [number, number, number];
  dark: [number, number, number];
  jump: [number, number, number];
  gap: [number, number, number];
  track: [number, number, number];
  coverage: [number, number, number];
}
// Each pair is checked for at least 3:1 contrast against its own sea colour (see styles.css).
const LIGHT: Palette = { land: [226, 222, 211], coast: [150, 144, 130], moving: [0, 96, 122], still: [92, 100, 112], dark: [176, 0, 64],
  jump: [168, 84, 0], gap: [110, 110, 120], track: [0, 96, 122], coverage: [40, 110, 200] }; // prettier-ignore
const DARK: Palette = { land: [38, 44, 54], coast: [90, 100, 116], moving: [84, 214, 224], still: [150, 160, 176], dark: [255, 110, 150],
  jump: [255, 176, 74], gap: [160, 164, 176], track: [84, 214, 224], coverage: [90, 160, 255] }; // prettier-ignore

export function palette(): Palette {
  return typeof matchMedia === "function" && matchMedia("(prefers-color-scheme: dark)").matches ? DARK : LIGHT;
}

export interface DeckOptions {
  initial: MapViewState;
  /** Called every animation frame; return the layers to draw above the land. Keep it allocation-light. */
  frame: (colors: Palette) => Layer[];
  onClick?: (info: PickingInfo) => void;
  tooltip?: (info: PickingInfo) => string | null;
  onView?: (view: MapViewState & { width: number; height: number }) => void;
  /** Pushes rAF intervals (ms) here when set: the frame-rate evidence. */
  frameLog?: number[];
}

/** One deck.gl instance bound to a div, redrawn on a requestAnimationFrame loop outside React's render cycle. */
export function useDeck(container: RefObject<HTMLDivElement | null>, options: DeckOptions): RefObject<Deck<MapView> | null> {
  const deck = useRef<Deck<MapView> | null>(null);
  const latest = useRef(options);
  latest.current = options;

  useEffect(() => {
    if (!container.current) return;
    const colors = palette();
    const land = new GeoJsonLayer({
      id: "land", data: `${import.meta.env.BASE_URL}land.geojson`, stroked: true, filled: true,
      getFillColor: colors.land, getLineColor: colors.coast, lineWidthMinPixels: 0.5,
    }); // prettier-ignore
    let instance: Deck<MapView>;
    try {
      instance = new Deck({
        parent: container.current,
        views: new MapView({ repeat: true }),
        initialViewState: latest.current.initial,
        controller: { keyboard: true, dragRotate: false, touchRotate: false },
        layers: [land],
        onClick: (info) => latest.current.onClick?.(info),
        getTooltip: (info) => latest.current.tooltip?.(info) ?? null,
        onViewStateChange: ({ viewState }) => latest.current.onView?.({ ...viewState, width: instance.width, height: instance.height }),
        getCursor: ({ isDragging, isHovering }) => (isDragging ? "grabbing" : isHovering ? "pointer" : "grab"),
      });
    } catch {
      container.current.dataset.webgl = "unavailable"; // styles.css turns this into a readable notice
      return;
    }
    deck.current = instance;
    let raf = 0;
    let last = performance.now();
    const loop = (now: number) => {
      latest.current.frameLog?.push(now - last);
      last = now;
      instance.setProps({ layers: [land, ...latest.current.frame(colors)] });
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => {
      cancelAnimationFrame(raf);
      instance.finalize();
      deck.current = null;
    };
  }, [container]);
  return deck;
}

/** The lon/lat box a view shows, as the API's bbox parameter. */
export function bboxOf(view: { longitude: number; latitude: number; zoom: number; width: number; height: number }): string {
  const degPerPx = 360 / (512 * 2 ** view.zoom);
  const halfLon = Math.min(180, (view.width / 2) * degPerPx);
  const halfLat = Math.min(85, (view.height / 2) * degPerPx * Math.cos((view.latitude * Math.PI) / 180));
  const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));
  const w = clamp(view.longitude - halfLon, -180, 179);
  const s = clamp(view.latitude - halfLat, -90, 89);
  return [w, s, clamp(view.longitude + halfLon, w + 0.01, 180), clamp(view.latitude + halfLat, s + 0.01, 90)].map((v) => v.toFixed(2)).join(",");
}
