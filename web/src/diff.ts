/**
 * A line diff for document changes (product-design.md §23.4): what was removed and added, with a little unchanged
 * text around each change and the rest folded. Common lines at both ends are matched first; the part in between by
 * longest common subsequence, or, when that would be too large to work out here, as removed then added.
 */

export type DiffLine = { kind: "same" | "add" | "del"; text: string };
export type DiffRow = DiffLine | { kind: "skip"; count: number };

const MAX_CELLS = 4_000_000;

export function diffLines(before: string, after: string): DiffLine[] {
  const a = before.split("\n");
  const b = after.split("\n");
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start++;
  let endA = a.length;
  let endB = b.length;
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) {
    endA--;
    endB--;
  }
  const same = (text: string): DiffLine => ({ kind: "same", text });
  const midA = a.slice(start, endA);
  const midB = b.slice(start, endB);
  const middle = midA.length * midB.length > MAX_CELLS
    ? [...midA.map((text): DiffLine => ({ kind: "del", text })), ...midB.map((text): DiffLine => ({ kind: "add", text }))]
    : lcs(midA, midB);
  return [...a.slice(0, start).map(same), ...middle, ...a.slice(endA).map(same)];
}

function lcs(a: string[], b: string[]): DiffLine[] {
  const n = a.length;
  const m = b.length;
  // len[i][j]: the longest common subsequence of a[i:] and b[j:]
  const len = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      len[i][j] = a[i] === b[j] ? len[i + 1][j + 1] + 1 : Math.max(len[i + 1][j], len[i][j + 1]);
    }
  }
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      out.push({ kind: "same", text: a[i] });
      i++;
      j++;
    } else if (len[i + 1][j] >= len[i][j + 1]) {
      out.push({ kind: "del", text: a[i++] });
    } else {
      out.push({ kind: "add", text: b[j++] });
    }
  }
  while (i < n) out.push({ kind: "del", text: a[i++] });
  while (j < m) out.push({ kind: "add", text: b[j++] });
  return out;
}

/** The changed lines with `context` unchanged lines around each; longer unchanged stretches folded. */
export function withContext(lines: DiffLine[], context = 2): DiffRow[] {
  const keep = lines.map(() => false);
  lines.forEach((line, i) => {
    if (line.kind === "same") return;
    for (let k = Math.max(0, i - context); k <= Math.min(lines.length - 1, i + context); k++) keep[k] = true;
  });
  const out: DiffRow[] = [];
  let skipped = 0;
  lines.forEach((line, i) => {
    if (!keep[i]) {
      skipped++;
      return;
    }
    if (skipped) out.push({ kind: "skip", count: skipped });
    skipped = 0;
    out.push(line);
  });
  if (skipped) out.push({ kind: "skip", count: skipped });
  return out;
}

export function diffCounts(lines: DiffLine[]): { added: number; removed: number } {
  return {
    added: lines.filter((l) => l.kind === "add").length,
    removed: lines.filter((l) => l.kind === "del").length,
  };
}
