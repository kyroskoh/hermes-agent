/**
 * Tiny line-level Myers diff for the config YAML preview.
 *
 * Computes the shortest edit script that turns `before` into `after` as a
 * list of `{op, text, beforeLine, afterLine}` rows.  op ∈ "same" | "del"
 * | "add".  Algorithm is Myers' O((N+M)·D) — fine for config.yaml.
 *
 * Why not a dep?  The Hermes web bundle is already big; a 60-line Myers
 * implementation is cheaper than pulling in `diff` (and its types).  The
 * existing UI has no diff visualizer, so we own the surface end-to-end.
 */

export type DiffOp = "same" | "del" | "add";

export interface DiffRow {
  op: DiffOp;
  text: string;
  beforeLine: number | null;
  afterLine: number | null;
}

/**
 * Diff two arrays of lines.
 *
 * Implementation notes:
 *  • trace[d] is a Record<number, number> indexed by diagonal, holding the
 *    furthest x reached at edit distance d.
 *  • direction[d] records whether we arrived at each (d, k) via a "down"
 *    move (insertion: y += 1, x unchanged) or "up" move (deletion: x += 1).
 *    The back-trace needs this to decide whether the last step was an
 *    add or a del; otherwise it can't tell which one was the real edit
 *    when the boundaries lie on the same path.
 */
export function diffLines(before: string[], after: string[]): DiffRow[] {
  const n = before.length;
  const m = after.length;

  if (n === 0 && m === 0) return [];

  const max = n + m;
  const trace: Array<Record<number, number>> = [{}];
  // direction[d][k] = true if the move at (d, k) was "down" (an insertion
  // in `after`); false if "up" (a deletion from `before`).
  const direction: Array<Record<number, boolean>> = [{}];

  trace[0][0] = 0;

  let foundD = 0;
  outer: for (let d = 0; d <= max; d += 1) {
    if (d > 0) {
      trace.push({});
      direction.push({});
    }
    // Diagonals to visit at this d: k from -d to d, step 2.  At d=0 there's
    // only k=0; at d=0 the edit distance is 0 if the inputs already match.
    for (let k = -d; k <= d; k += 2) {
      let baseX: number;
      let down: boolean;
      if (d === 0) {
        baseX = 0;
        down = false; // k=0 at d=0 is the seed; no direction.
      } else {
        down =
          k === -d ||
          (k !== d &&
            (trace[d - 1][k - 1] ?? 0) < (trace[d - 1][k + 1] ?? 0));
        baseX = down
          ? (trace[d - 1][k + 1] ?? 0)
          : (trace[d - 1][k - 1] ?? 0) + 1;
      }
      let x = baseX;
      let y = x - k;
      while (x < n && y < m && before[x] === after[y]) {
        x += 1;
        y += 1;
      }
      trace[d][k] = x;
      if (d > 0) direction[d][k] = down;
      if (x >= n && y >= m) {
        foundD = d;
        break outer;
      }
    }
  }

  // Back-trace from (n, m) following the trace.
  const ops: Array<{ op: DiffOp; text: string }> = [];
  let x = n;
  let y = m;
  for (let d = foundD; d > 0; d -= 1) {
    const k = x - y;
    const prevK = direction[d][k] ? k + 1 : k - 1;
    const prevX = trace[d - 1][prevK] ?? 0;
    const prevY = prevX - prevK;

    while (x > prevX && y > prevY) {
      ops.push({ op: "same", text: before[x - 1] });
      x -= 1;
      y -= 1;
    }
    if (direction[d][k]) {
      // Insertion (added a line in `after`).
      ops.push({ op: "add", text: after[y - 1] });
      y -= 1;
    } else {
      // Deletion (removed a line from `before`).
      ops.push({ op: "del", text: before[x - 1] });
      x -= 1;
    }
  }
  while (x > 0 && y > 0) {
    ops.push({ op: "same", text: before[x - 1] });
    x -= 1;
    y -= 1;
  }
  while (x > 0) {
    ops.push({ op: "del", text: before[x - 1] });
    x -= 1;
  }
  while (y > 0) {
    ops.push({ op: "add", text: after[y - 1] });
    y -= 1;
  }
  ops.reverse();

  // Per-side line numbers in document order.
  let bLine = 1;
  let aLine = 1;
  const rows: DiffRow[] = [];
  for (const o of ops) {
    if (o.op === "same") {
      rows.push({
        op: "same",
        text: o.text,
        beforeLine: bLine,
        afterLine: aLine,
      });
      bLine += 1;
      aLine += 1;
    } else if (o.op === "del") {
      rows.push({
        op: "del",
        text: o.text,
        beforeLine: bLine,
        afterLine: null,
      });
      bLine += 1;
    } else {
      rows.push({
        op: "add",
        text: o.text,
        beforeLine: null,
        afterLine: aLine,
      });
      aLine += 1;
    }
  }
  return rows;
}

/** Split a YAML blob into lines.  Drops a single trailing empty line so
 * files ending in \n don't render an extra blank row. */
export function splitLines(text: string): string[] {
  const lines = text.split(/\r?\n/);
  if (lines.length > 0 && lines[lines.length - 1] === "") {
    lines.pop();
  }
  return lines;
}

/**
 * Group a flat diff row list into hunks with `contextLines` of unchanged
 * context on each side (matches `git diff -U`).  Pure-unchanged runs are
 * dropped; consecutive changes stay in one hunk.
 */
export function groupIntoHunks(
  rows: DiffRow[],
  contextLines = 3,
): Array<{
  header: {
    beforeStart: number;
    beforeCount: number;
    afterStart: number;
    afterCount: number;
  };
  rows: DiffRow[];
}> {
  const changedIndices: number[] = [];
  rows.forEach((r, i) => {
    if (r.op !== "same") changedIndices.push(i);
  });

  if (changedIndices.length === 0) return [];

  const hunks: Array<DiffRow[]> = [];
  let groupStart = changedIndices[0];
  let groupEnd = changedIndices[0];
  for (let k = 1; k < changedIndices.length; k += 1) {
    const idx = changedIndices[k];
    if (idx - groupEnd <= contextLines * 2 + 1) {
      groupEnd = idx;
    } else {
      hunks.push(
        rows.slice(
          Math.max(0, groupStart - contextLines),
          Math.min(rows.length, groupEnd + contextLines + 1),
        ),
      );
      groupStart = idx;
      groupEnd = idx;
    }
  }
  hunks.push(
    rows.slice(
      Math.max(0, groupStart - contextLines),
      Math.min(rows.length, groupEnd + contextLines + 1),
    ),
  );

  return hunks.map((hunkRows) => {
    const firstBefore = hunkRows.find((r) => r.beforeLine !== null);
    const lastBefore = [...hunkRows]
      .reverse()
      .find((r) => r.beforeLine !== null);
    const firstAfter = hunkRows.find((r) => r.afterLine !== null);
    const lastAfter = [...hunkRows]
      .reverse()
      .find((r) => r.afterLine !== null);
    const beforeStart = firstBefore?.beforeLine ?? 1;
    const afterStart = firstAfter?.afterLine ?? 1;
    const beforeCount =
      (lastBefore?.beforeLine ?? 0) - beforeStart + (lastBefore ? 1 : 0);
    const afterCount =
      (lastAfter?.afterLine ?? 0) - afterStart + (lastAfter ? 1 : 0);
    return {
      header: { beforeStart, beforeCount, afterStart, afterCount },
      rows: hunkRows,
    };
  });
}