// Colours. Categorical slots and the sequential blue ramp are a palette validated for
// colour-vision deficiency and contrast against these two surfaces (see docs/DECISIONS.md, F6).

import type { RGB, Theme } from "./renderer";

const hex = (h: string): RGB => [parseInt(h.slice(1, 3), 16) / 255, parseInt(h.slice(3, 5), 16) / 255, parseInt(h.slice(5, 7), 16) / 255];

export type ThemeName = "light" | "dark";

export const SLOTS: Record<ThemeName, string[]> = {
  light: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"],
  dark: ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"],
};
/** Every category past the eighth, and "(none)", shares this grey: a ninth hue would not be tellable apart. */
export const OTHER: Record<ThemeName, string> = { light: "#898781", dark: "#898781" };

export const THEMES: Record<ThemeName, Theme> = {
  light: { bg: hex("#fcfcfb"), muted: hex("#c3c2b7"), ramp: ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#0d366b"].map(hex) },
  dark: { bg: hex("#1a1a19"), muted: hex("#5a5955"), ramp: ["#184f95", "#256abf", "#3987e5", "#86b6ef", "#cde2fb"].map(hex) },
};

/** Colour of a category code. Codes are assigned by falling frequency, so the eight largest get a hue. */
export const categoryColor = (theme: ThemeName, code: number): string => (code >= 1 && code <= 8 ? SLOTS[theme][code - 1] : OTHER[theme]);

/** The 256x4 RGBA texture the vertex shader reads: rgb = colour, alpha = passes the filter. */
export function paletteTexture(theme: ThemeName, allow: Uint8Array | null): Uint8Array {
  const data = new Uint8Array(256 * 4 * 4);
  for (let row = 0; row < 4; row++)
    for (let code = 0; code < 256; code++) {
      const [r, g, b] = hex(categoryColor(theme, code));
      const o = (row * 256 + code) * 4;
      data[o] = r * 255;
      data[o + 1] = g * 255;
      data[o + 2] = b * 255;
      data[o + 3] = !allow || row > 2 || allow[row * 256 + code] ? 255 : 0;
    }
  return data;
}
