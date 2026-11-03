import { describe, expect, it } from "vitest";
import { diffCounts, diffLines, withContext } from "./diff";

describe("document diffs", () => {
  it("finds the changed lines between unchanged ones", () => {
    expect(diffLines("a\nb\nc", "a\nB\nc\nd")).toEqual([
      { kind: "same", text: "a" }, { kind: "del", text: "b" }, { kind: "add", text: "B" },
      { kind: "same", text: "c" }, { kind: "add", text: "d" },
    ]);
    expect(diffCounts(diffLines("a\nb", "a\nc\nd"))).toEqual({ added: 2, removed: 1 });
  });

  it("keeps two lines around a change and folds the rest", () => {
    const before = Array.from({ length: 20 }, (_, i) => `line ${i}`).join("\n");
    const rows = withContext(diffLines(before, before.replace("line 10", "line ten")));
    expect(rows).toEqual([
      { kind: "skip", count: 8 }, { kind: "same", text: "line 8" }, { kind: "same", text: "line 9" },
      { kind: "del", text: "line 10" }, { kind: "add", text: "line ten" },
      { kind: "same", text: "line 11" }, { kind: "same", text: "line 12" }, { kind: "skip", count: 7 },
    ]);
  });

  it("gives up on matching inside a very large change instead of stalling", () => {
    const a = Array.from({ length: 2500 }, (_, i) => `a${i}`).join("\n");
    const b = Array.from({ length: 2500 }, (_, i) => `b${i}`).join("\n");
    const lines = diffLines(a, b);
    expect(diffCounts(lines)).toEqual({ added: 2500, removed: 2500 });
    expect(lines[0]).toEqual({ kind: "del", text: "a0" });
  });
});
