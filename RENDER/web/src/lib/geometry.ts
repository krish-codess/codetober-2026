// Polygons are flat [x0, y0, x1, y1, ...] in projection units, closed implicitly, even-odd rule.

/**
 * Even-odd ray casting. dots/api.py `points_in_polygon` evaluates the same expression in the same
 * order in float64, so the two agree bit for bit, including for points exactly on an edge.
 */
export function pointInPolygon(x: number, y: number, poly: ArrayLike<number>): boolean {
  let inside = false;
  const n = poly.length;
  let xj = poly[n - 2], yj = poly[n - 1];
  for (let i = 0; i < n; i += 2) {
    const xi = poly[i], yi = poly[i + 1];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
    xj = xi;
    yj = yi;
  }
  return inside;
}

export const OUTSIDE = 0, INSIDE = 1, PARTIAL = 2;

/**
 * Where an axis-aligned rectangle sits relative to a polygon. Conservative: it answers PARTIAL
 * whenever an edge comes within rounding distance of the rectangle, so INSIDE and OUTSIDE can be
 * trusted for every point in the rectangle without testing them one by one.
 */
export function classifyRect(poly: ArrayLike<number>, x0: number, y0: number, x1: number, y1: number): number {
  const n = poly.length;
  const xMag = Math.abs(x0) + Math.abs(x1), yMag = Math.abs(y0) + Math.abs(y1);
  let xj = poly[n - 2], yj = poly[n - 1];
  for (let i = 0; i < n; i += 2) {
    const xi = poly[i], yi = poly[i + 1];
    // The edge can only matter if its bounding box comes within rounding distance of the rectangle.
    // pointInPolygon places a crossing only to within a few ulps of the edge's own coordinates
    // (its "+ xi" cancels), so "within rounding distance" is measured in those, not in the gap.
    const ex = 4e-15 * (xMag + Math.abs(xi) + Math.abs(xj)) + 1e-300, ey = 4e-15 * (yMag + Math.abs(yi) + Math.abs(yj)) + 1e-300;
    if (Math.max(xi, xj) >= x0 - ex && Math.min(xi, xj) <= x1 + ex && Math.max(yi, yj) >= y0 - ey && Math.min(yi, yj) <= y1 + ey) {
      // ...and the four corners are not all strictly on one side of the edge's line. "Strictly"
      // means by more than rounding could account for, here or in pointInPolygon: a few ulps of
      // the largest terms involved, plus a floor so products that underflow still count as zero.
      const dx = xj - xi, dy = yj - yi;
      const tol = 4e-15 * (Math.abs(dx) * (yMag + Math.abs(yi) + Math.abs(yj)) + Math.abs(dy) * (xMag + Math.abs(xi) + Math.abs(xj))) + 1e-300;
      const a = dx * (y0 - yi) - dy * (x0 - xi);
      const b = dx * (y0 - yi) - dy * (x1 - xi);
      const c = dx * (y1 - yi) - dy * (x0 - xi);
      const d = dx * (y1 - yi) - dy * (x1 - xi);
      if (!((a > tol && b > tol && c > tol && d > tol) || (a < -tol && b < -tol && c < -tol && d < -tol))) return PARTIAL;
    }
    xj = xi;
    yj = yi;
  }
  return pointInPolygon((x0 + x1) / 2, (y0 + y1) / 2, poly) ? INSIDE : OUTSIDE;
}

export function bboxOf(poly: ArrayLike<number>): [number, number, number, number] {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (let i = 0; i < poly.length; i += 2) {
    x0 = Math.min(x0, poly[i]);
    x1 = Math.max(x1, poly[i]);
    y0 = Math.min(y0, poly[i + 1]);
    y1 = Math.max(y1, poly[i + 1]);
  }
  return [x0, y0, x1, y1];
}

/** Drop lasso vertices closer than `minDist` to the previous kept one, so a slow drag stays a small polygon. */
export function simplify(poly: number[], minDist: number, maxVertices = 512): number[] {
  for (;;) {
    const out = [poly[0], poly[1]];
    for (let i = 2; i < poly.length; i += 2) {
      const dx = poly[i] - out[out.length - 2], dy = poly[i + 1] - out[out.length - 1];
      if (dx * dx + dy * dy >= minDist * minDist) out.push(poly[i], poly[i + 1]);
    }
    if (out.length <= maxVertices * 2) return out;
    minDist = Math.max(minDist * 1.5, 1e-12);
  }
}
