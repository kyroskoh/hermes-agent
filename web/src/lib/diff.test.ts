/**
 * Unit tests for the line-level Myers diff used by the config YAML diff
 * modal.  These are quick correctness checks — the modal itself is hard to
 * test without a DOM, so the algorithm is the load-bearing piece.
 */
import { describe, it, expect } from "vitest";
import { diffLines, splitLines, groupIntoHunks } from "./diff";

describe("diffLines", () => {
  it("returns empty rows for two empty inputs", () => {
    expect(diffLines([], [])).toEqual([]);
  });

  it("marks identical lines as 'same' with sequential line numbers", () => {
    const rows = diffLines(["a", "b", "c"], ["a", "b", "c"]);
    expect(rows).toEqual([
      { op: "same", text: "a", beforeLine: 1, afterLine: 1 },
      { op: "same", text: "b", beforeLine: 2, afterLine: 2 },
      { op: "same", text: "c", beforeLine: 3, afterLine: 3 },
    ]);
  });

  it("reports pure additions", () => {
    const rows = diffLines(["a"], ["a", "b", "c"]);
    expect(rows.map((r) => r.op)).toEqual(["same", "add", "add"]);
    expect(rows[0].beforeLine).toBe(1);
    expect(rows[1].afterLine).toBe(2);
    expect(rows[2].afterLine).toBe(3);
  });

  it("reports pure deletions", () => {
    const rows = diffLines(["a", "b", "c"], ["a"]);
    expect(rows.map((r) => r.op)).toEqual(["same", "del", "del"]);
    expect(rows[0].beforeLine).toBe(1);
    expect(rows[1].beforeLine).toBe(2);
    expect(rows[2].beforeLine).toBe(3);
  });

  it("reports a single-line replacement as one del + one add", () => {
    const rows = diffLines(["x = 1", "y = 2"], ["x = 1", "y = 99"]);
    expect(rows[0]).toMatchObject({ op: "same", text: "x = 1" });
    expect(rows[1].op).toBe("del");
    expect(rows[1].text).toBe("y = 2");
    expect(rows[2].op).toBe("add");
    expect(rows[2].text).toBe("y = 99");
  });

  it("tracks both line numbers correctly across mixed ops", () => {
    const before = ["# header", "value: 1", "other: x"];
    const after = ["# header", "value: 2", "extra: y", "other: x"];
    const rows = diffLines(before, after);
    // Three changes: del value:1, add value:2, add extra:y.  Two anchors.
    const ops = rows.map((r) => r.op);
    expect(ops.filter((o) => o === "same").length).toBe(2);
    expect(ops.filter((o) => o !== "same").length).toBe(3);
    // Find the "same" # header row and the "same" other: x row — they
    // should both have non-null line numbers.
    const sameHeader = rows.find((r) => r.text === "# header");
    const sameOther = rows.find((r) => r.text === "other: x");
    expect(sameHeader?.beforeLine).toBe(1);
    expect(sameHeader?.afterLine).toBe(1);
    expect(sameOther?.beforeLine).toBe(3);
    expect(sameOther?.afterLine).toBe(4);
  });

  it("produces a stable diff (insertion order is consistent)", () => {
    const before = ["a", "b", "c", "d", "e"];
    const after = ["a", "c", "d", "e"];
    // Removing 'b' should give a single del — never a del+add combination
    // for the same logical change.
    const rows = diffLines(before, after);
    const ops = rows.map((r) => r.op);
    expect(ops.filter((o) => o === "del").length).toBe(1);
    expect(ops.filter((o) => o === "add").length).toBe(0);
  });
});

describe("splitLines", () => {
  it("returns an empty array for empty input", () => {
    expect(splitLines("")).toEqual([]);
  });

  it("splits on \\n", () => {
    expect(splitLines("a\nb\nc")).toEqual(["a", "b", "c"]);
  });

  it("splits on \\r\\n", () => {
    expect(splitLines("a\r\nb\r\nc")).toEqual(["a", "b", "c"]);
  });

  it("drops a single trailing empty entry (file ended with newline)", () => {
    expect(splitLines("a\nb\n")).toEqual(["a", "b"]);
  });

  it("preserves interior empty lines", () => {
    expect(splitLines("a\n\nb")).toEqual(["a", "", "b"]);
  });
});

describe("groupIntoHunks", () => {
  it("returns no hunks when there are no changes", () => {
    const rows = diffLines(["a", "b"], ["a", "b"]);
    expect(groupIntoHunks(rows)).toEqual([]);
  });

  it("groups adjacent changes into one hunk", () => {
    const rows = diffLines(["a", "b", "c"], ["a", "X", "Y"]);
    const hunks = groupIntoHunks(rows);
    expect(hunks.length).toBe(1);
    expect(hunks[0].rows.some((r) => r.op !== "same")).toBe(true);
  });

  it("splits distant changes into separate hunks with context", () => {
    const before = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"];
    const after = ["a", "b", "c", "d", "X", "f", "g", "h", "i", "Y"];
    const rows = diffLines(before, after);
    // Default context=3 → hunks at indices 4 and 9 are within context of
    // each other and merge.  Use context=0 to force a split.
    const hunks = groupIntoHunks(rows, 0);
    expect(hunks.length).toBe(2);
  });

  it("each hunk header has consistent beforeStart/afterStart numbers", () => {
    const before = ["a", "b", "c", "d", "e"];
    const after = ["a", "b", "X", "d", "e"];
    const rows = diffLines(before, after);
    const hunks = groupIntoHunks(rows, 3);
    expect(hunks.length).toBe(1);
    const h = hunks[0];
    expect(h.header.beforeStart).toBeGreaterThan(0);
    expect(h.header.afterStart).toBeGreaterThan(0);
  });
});