/**
 * A uniform grid over one table's bounding boxes.
 *
 * This is the index the pack does not ship. `engine.search._CANDIDATE_SQL` and
 * `geocode.reverse._SEGMENTS_IN_BBOX_SQL` are four comparisons against
 * `ix_reg_seg_bbox` / `ix_street_segment_bbox`; without an index behind them
 * the browser would test every one of the 36,172 spans on every search.
 *
 * `query` returns a *superset*: every row filed in a cell the query box
 * touches, in ascending row order. The caller applies the same four
 * comparisons the SQL did, so a row that only shares a cell is rejected there.
 */

// engine/geo.py's constants, and the latitude its comment is written for.
const M_PER_DEG_LAT = 111132.0;
const M_PER_DEG_LON_AT_EQUATOR = 111320.0;
const GRID_LAT_DEG = 40.75;

// A curb span is tens of metres and a walk radius is hundreds, so a 100 m cell
// puts most rows in one or two cells and a 5-minute walk in about a hundred.
const CELL_M = 100;

export const CELL_LAT_DEG = CELL_M / M_PER_DEG_LAT;
export const CELL_LON_DEG =
  CELL_M / (M_PER_DEG_LON_AT_EQUATOR * Math.cos(GRID_LAT_DEG * (Math.PI / 180)));

// Manhattan at 100 m is about 130 x 300 cells. The cap only matters if a pack
// ever carried a stray coordinate far from the rest: the grid then coarsens
// instead of asking for billions of cells, and the answer is still a superset.
const MAX_CELLS = 1 << 20;

const NO_ROWS = new Int32Array(0);

/**
 * @typedef {{minLon: Float64Array, minLat: Float64Array, maxLon: Float64Array, maxLat: Float64Array}} Boxes
 */

/**
 * File `n` bounding boxes into a grid.
 *
 * @param {Boxes} boxes per-row bounds, as `loader.js` derives them from the geometry
 * @param {number} n row count
 * @returns {{cols: number, rows: number, cellLon: number, cellLat: number,
 *   query: (minLon: number, minLat: number, maxLon: number, maxLat: number) => Int32Array}}
 */
export function buildGrid(boxes, n) {
  const extent = tableExtent(boxes, n);
  let cellLon = CELL_LON_DEG;
  let cellLat = CELL_LAT_DEG;
  let cols = cellSpan(extent.maxLon - extent.minLon, cellLon);
  let rows = cellSpan(extent.maxLat - extent.minLat, cellLat);
  while (cols * rows > MAX_CELLS) {
    cellLon *= 2;
    cellLat *= 2;
    cols = cellSpan(extent.maxLon - extent.minLon, cellLon);
    rows = cellSpan(extent.maxLat - extent.minLat, cellLat);
  }

  const colOf = (lon) => clamp(Math.floor((lon - extent.minLon) / cellLon), 0, cols - 1);
  const rowOf = (lat) => clamp(Math.floor((lat - extent.minLat) / cellLat), 0, rows - 1);

  // Compressed sparse rows: count per cell, prefix-sum into starts, then fill
  // in ascending row order so each cell's rows are already ascending.
  const total = cols * rows;
  const starts = new Int32Array(total + 1);
  for (let row = 0; row < n; row += 1) {
    forEachCell(boxes, row, colOf, rowOf, cols, (cell) => {
      starts[cell + 1] += 1;
    });
  }
  for (let cell = 1; cell <= total; cell += 1) {
    starts[cell] += starts[cell - 1];
  }
  const items = new Int32Array(starts[total]);
  const cursor = starts.slice(0, total);
  for (let row = 0; row < n; row += 1) {
    forEachCell(boxes, row, colOf, rowOf, cols, (cell) => {
      items[cursor[cell]] = row;
      cursor[cell] += 1;
    });
  }

  // A row in several cells is reached several times; `seen` marks it for this
  // query instead of allocating a Set per call.
  const seen = new Int32Array(n).fill(-1);
  let pass = 0;

  function query(minLon, minLat, maxLon, maxLat) {
    if (
      n === 0 ||
      maxLon < extent.minLon ||
      minLon > extent.maxLon ||
      maxLat < extent.minLat ||
      minLat > extent.maxLat
    ) {
      return NO_ROWS;
    }
    pass += 1;
    const found = [];
    const firstCol = colOf(minLon);
    const lastCol = colOf(maxLon);
    const firstRow = rowOf(minLat);
    const lastRow = rowOf(maxLat);
    for (let gridRow = firstRow; gridRow <= lastRow; gridRow += 1) {
      for (let gridCol = firstCol; gridCol <= lastCol; gridCol += 1) {
        const cell = gridRow * cols + gridCol;
        for (let at = starts[cell]; at < starts[cell + 1]; at += 1) {
          const row = items[at];
          if (seen[row] === pass) {
            continue;
          }
          seen[row] = pass;
          found.push(row);
        }
      }
    }
    const candidates = Int32Array.from(found);
    // Typed-array sort is numeric, and the caller reads rows in row order.
    candidates.sort();
    return candidates;
  }

  return { cols, rows, cellLon, cellLat, query };
}

function forEachCell(boxes, row, colOf, rowOf, cols, visit) {
  const firstCol = colOf(boxes.minLon[row]);
  const lastCol = colOf(boxes.maxLon[row]);
  const firstRow = rowOf(boxes.minLat[row]);
  const lastRow = rowOf(boxes.maxLat[row]);
  for (let gridRow = firstRow; gridRow <= lastRow; gridRow += 1) {
    for (let gridCol = firstCol; gridCol <= lastCol; gridCol += 1) {
      visit(gridRow * cols + gridCol);
    }
  }
}

function tableExtent(boxes, n) {
  let minLon = Infinity;
  let minLat = Infinity;
  let maxLon = -Infinity;
  let maxLat = -Infinity;
  for (let row = 0; row < n; row += 1) {
    minLon = Math.min(minLon, boxes.minLon[row]);
    minLat = Math.min(minLat, boxes.minLat[row]);
    maxLon = Math.max(maxLon, boxes.maxLon[row]);
    maxLat = Math.max(maxLat, boxes.maxLat[row]);
  }
  if (n === 0) {
    return { minLon: 0, minLat: 0, maxLon: 0, maxLat: 0 };
  }
  return { minLon, minLat, maxLon, maxLat };
}

function cellSpan(span, size) {
  return Math.max(1, Math.floor(span / size) + 1);
}

function clamp(value, low, high) {
  if (value < low) {
    return low;
  }
  return value > high ? high : value;
}
