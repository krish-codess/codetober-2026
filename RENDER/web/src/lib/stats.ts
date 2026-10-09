// Summary statistics of a selection from its per-category counts. Mirrors dots/api.py summarise_counts.

import type { Column } from "./types";

export interface CategoryCount {
  code: number;
  label: string;
  count: number;
  /** count / n */
  share: number;
  /** share in the selection over share in the whole dataset; null when the label is absent overall */
  lift: number | null;
}

export interface ColumnSummary {
  name: string;
  /** share of the most common label; null for an empty selection */
  purity: number | null;
  /** Shannon entropy normalised to 0..1 by log(labels present overall) */
  entropy: number | null;
  counts: CategoryCount[];
}

export function summarise(columns: Column[], counts: ArrayLike<number>[], total: number): ColumnSummary[] {
  return columns.map((col, ci) => {
    const c = counts[ci];
    let n = 0;
    for (let code = 0; code < col.labels.length; code++) n += c[code];
    const items: CategoryCount[] = [];
    let entropy = 0, top = 0;
    for (let code = 0; code < col.labels.length; code++) {
      if (!c[code]) continue;
      const share = c[code] / n;
      entropy -= share * Math.log(share);
      top = Math.max(top, share);
      items.push({ code, label: col.labels[code], count: c[code], share, lift: col.counts[code] ? share / (col.counts[code] / total) : null });
    }
    items.sort((a, b) => b.count - a.count || (a.label < b.label ? -1 : 1));
    const present = col.counts.filter((v) => v > 0).length;
    return { name: col.name, purity: n ? top : null, entropy: n ? (present > 1 ? entropy / Math.log(present) : 0) : null, counts: items };
  });
}

export const formatInt = (n: number): string => Math.round(n).toLocaleString("en-US");
export const formatShare = (p: number): string => (p >= 0.9995 ? "100%" : p < 0.001 && p > 0 ? "<0.1%" : `${(p * 100).toFixed(1)}%`);
