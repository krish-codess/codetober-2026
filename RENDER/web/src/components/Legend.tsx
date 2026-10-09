import { formatInt } from "../lib/stats";
import { categoryColor, type ThemeName } from "../lib/theme";
import type { Column } from "../lib/types";

interface Props {
  column: Column;
  theme: ThemeName;
  /** show a colour swatch per category (off when the plot is coloured by something else) */
  coloured: boolean;
  hidden: ReadonlySet<number>;
  onToggle: (code: number) => void;
  onAll: (show: boolean) => void;
}

/** Legend and filter in one: each category is a checkbox, labelled in text with its count, swatch beside it. */
export function Legend({ column, theme, coloured, hidden, onToggle, onAll }: Props) {
  const codes = column.labels.map((_, code) => code).filter((code) => column.counts[code] > 0);
  return (
    <fieldset className="legend">
      <legend>
        Filter by {column.name}
        <span className="muted small"> ({codes.length - hidden.size} of {codes.length} shown)</span>
      </legend>
      <div className="row">
        <button onClick={() => onAll(true)} disabled={hidden.size === 0}>
          Show all
        </button>
        <button onClick={() => onAll(false)} disabled={hidden.size === codes.length}>
          Hide all
        </button>
      </div>
      <ul>
        {codes.map((code) => (
          <li key={code}>
            <label>
              <input type="checkbox" checked={!hidden.has(code)} onChange={() => onToggle(code)} />
              {coloured && <span className="swatch" style={{ background: categoryColor(theme, code) }} aria-hidden="true" />}
              <bdi className="label" title={column.labels[code]}>
                {column.labels[code]}
              </bdi>
              <span className="muted small">{formatInt(column.counts[code])}</span>
            </label>
          </li>
        ))}
      </ul>
      {coloured && codes.some((c) => c > 8 || c === 0) && <p className="muted small">Only the eight largest categories get their own colour; the rest share grey. The labels and the hover card always name the category.</p>}
    </fieldset>
  );
}
