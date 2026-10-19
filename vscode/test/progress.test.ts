import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { describe, expect, it } from "vitest";
import { findJobLog, liveJobs, logProgress } from "../src/progress";

describe("logProgress", () => {
  it("counts per-test lines from both runners and keeps the last test seen", () => {
    const p = logProgress([
      "build output",
      "passed Acme.Tests.One (3ms)",
      "failed Acme.Tests.Two (1ms)",
      "  from /work/x.dll",
      "  Passed Acme.Tests.Three [1 ms]",
      "  Failed Acme.Tests.Four [2 ms]",
      "skipped Acme.Tests.Five",
      "",
    ].join("\n"));
    expect(p).toMatchObject({ passed: 2, failed: 2, skipped: 1, lastTest: "Acme.Tests.Five" });
    expect(p.tail).toEqual(["  Passed Acme.Tests.Three [1 ms]", "  Failed Acme.Tests.Four [2 ms]", "skipped Acme.Tests.Five"]);
  });

  it("gives zeros and no last test while the build runs", () => {
    expect(logProgress("Restoring...\nBuilding...\n")).toMatchObject({ passed: 0, failed: 0, skipped: 0, lastTest: "" });
  });
});

describe("findJobLog", () => {
  it("finds the job's log in the newest run folder that has it", () => {
    const base = fs.mkdtempSync(path.join(os.tmpdir(), "tp-"));
    for (const [dir, file] of [["20261019-100000", "job007-A.csproj-full.log"], ["20261019-120000", "job007-A.csproj-full.log"],
      ["20261019-130000", "job008-B.csproj-fast.log"]]) {
      fs.mkdirSync(path.join(base, dir), { recursive: true });
      fs.writeFileSync(path.join(base, dir, file), "x");
    }
    expect(findJobLog(base, 7)).toBe(path.join(base, "20261019-120000", "job007-A.csproj-full.log"));
    expect(findJobLog(base, 1234)).toBeUndefined();
    expect(findJobLog(path.join(base, "missing"), 7)).toBeUndefined();
  });
});

describe("liveJobs", () => {
  it("reads each running job's log and says how long it has been quiet", () => {
    const base = fs.mkdtempSync(path.join(os.tmpdir(), "tp-"));
    fs.mkdirSync(path.join(base, "r1"));
    const log = path.join(base, "r1", "job042-A.csproj-full.log");
    fs.writeFileSync(log, "failed A.T.One (1ms)\n");
    const now = Math.floor(fs.statSync(log).mtimeMs / 1000) + 30;
    const job = { id: 42, started_at: new Date((now - 600) * 1000).toISOString(), ended_at: null } as never;
    const missing = { id: 43, started_at: null, ended_at: null } as never;
    const [a, b] = liveJobs([job, missing], base, now);
    expect(a).toMatchObject({ id: 42, failed: 1, elapsed: 600, quietFor: 30, lastTest: "A.T.One" });
    expect(b).toMatchObject({ id: 43, failed: 0, elapsed: null, quietFor: null, tail: [] });
  });
});
