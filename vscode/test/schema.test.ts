import * as fs from "node:fs";
import * as path from "node:path";
import { describe, expect, it } from "vitest";
import { emptyDb } from "./fixture";

const BRAIN_LIB = path.join(__dirname, "..", "..", "lib", "brain");
/** brain's source: bin/brain only starts the package in lib/brain. */
const brainSource = () =>
  fs.readdirSync(BRAIN_LIB).filter((f) => f.endsWith(".py")).sort()
    .map((f) => fs.readFileSync(path.join(BRAIN_LIB, f), "utf8")).join("\n");
const TABLES = ["meta", "runs", "items", "tags", "jobs", "sessions", "activities"];

/** Column names per table as written in lib/brain: CREATE TABLE blocks plus ALTER TABLE ... ADD COLUMN. */
function brainColumns(text: string): Record<string, string[]> {
  const src = text.replace(/"\s*\n\s*"/g, ""); // join python string pieces
  const out: Record<string, string[]> = {};
  const re = /CREATE TABLE (?:IF NOT EXISTS )?(\w+) \(/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(src))) {
    const name = m[1];
    if (!TABLES.includes(name)) continue;
    let depth = 1, i = re.lastIndex;
    const start = i;
    while (depth && i < src.length) {
      if (src[i] === "(") depth++;
      else if (src[i] === ")") depth--;
      i++;
    }
    const body = src.slice(start, i - 1);
    const parts: string[] = [];
    let d = 0, cur = "";
    for (const ch of body) {
      if (ch === "(") d++;
      if (ch === ")") d--;
      if (ch === "," && d === 0) { parts.push(cur); cur = ""; } else cur += ch;
    }
    parts.push(cur);
    out[name] = parts.map((p) => p.trim().split(/\s+/)[0]).filter((c) => !/^(PRIMARY|FOREIGN|UNIQUE)$/i.test(c));
  }
  const alter = /ALTER TABLE (\w+) ADD COLUMN (\w+)/g;
  while ((m = alter.exec(src))) if (TABLES.includes(m[1])) (out[m[1]] ??= []).push(m[2]);
  return out;
}

describe("fixture schema matches lib/brain", () => {
  it("has the same columns for every table", async () => {
    const theirs = brainColumns(brainSource());
    expect(Object.keys(theirs).sort()).toEqual([...TABLES].sort());
    const db = await emptyDb();
    for (const t of TABLES) {
      const res = db.exec(`PRAGMA table_info(${t})`)[0];
      const ours = res.values.map((r) => String(r[1]));
      expect(theirs[t].length).toBeGreaterThan(0);
      expect([...ours].sort(), `columns of ${t}`).toEqual([...theirs[t]].sort());
    }
  });

  it("is at the schema version the fixture says", () => {
    const text = brainSource();
    const n = /MIGRATIONS = \[(.*)\]/.exec(text)![1].split(",").length;
    expect(n).toBe(7);
  });
});
