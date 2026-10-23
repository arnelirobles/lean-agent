import * as fs from "fs";
import * as path from "path";
import { jobElapsed } from "./db";
import type { JobRow } from "./types";

/** What a running job's log says so far. Passed lines appear only when the runner prints them. */
export interface Progress { passed: number; failed: number; skipped: number; lastTest: string; tail: string[] }

/** A running job's progress as the dashboard shows it, sent to the webview every few seconds. */
export interface LiveJob extends Progress { id: number; elapsed: number | null; quietFor: number | null }

// "passed Name (3ms)" from Microsoft.Testing.Platform, "  Passed Name [1 ms]" from vstest, " \u2713 file > Name 2ms" from vitest.
const TEST_LINE = /^\s*(passed|failed|skipped|Passed|Failed|Skipped|[\u2713\u00d7\u2717\u2718\u2193]) ([^\s(\[]+)/;
const MARKS: Record<string, "passed" | "failed" | "skipped"> = {
  "\u2713": "passed", "\u00d7": "failed", "\u2717": "failed", "\u2718": "failed", "\u2193": "skipped",
};

export function logProgress(text: string): Progress {
  const p: Progress = { passed: 0, failed: 0, skipped: 0, lastTest: "", tail: [] };
  const lines = text.split("\n");
  for (const line of lines) {
    const m = TEST_LINE.exec(line);
    if (!m) continue;
    const kind = MARKS[m[1]] ?? (m[1].toLowerCase() as "passed" | "failed" | "skipped");
    p[kind]++;
    p.lastTest = m[2];
  }
  p.tail = lines.filter((l) => l.trim()).slice(-3);
  return p;
}

/** test-phase writes job<id, 3 digits>-<project>-<tier>.log in a run folder; the newest folder with one wins. */
export function findJobLog(base: string, id: number): string | undefined {
  let dirs: string[];
  try {
    dirs = fs.readdirSync(base).sort().reverse();
  } catch {
    return undefined;
  }
  const prefix = `job${String(id).padStart(3, "0")}-`;
  for (const d of dirs) {
    try {
      const hit = fs.readdirSync(path.join(base, d)).find((f) => f.startsWith(prefix) && f.endsWith(".log"));
      if (hit) return path.join(base, d, hit);
    } catch {
      // not a folder
    }
  }
  return undefined;
}

/** The test-phase run folder, as test-phase picks it. */
export function testPhaseDir(env: NodeJS.ProcessEnv, home: string): string {
  return env.LEAN_TEST_PHASE_DIR || path.join(home, ".local", "share", "lean", "test-phase");
}

/** Progress for each running job, read from its test-phase log. A job without a log yet gets zeros. */
export function liveJobs(running: JobRow[], base: string, now: number): LiveJob[] {
  return running.map((j) => {
    const log = findJobLog(base, j.id);
    let p: Progress = { passed: 0, failed: 0, skipped: 0, lastTest: "", tail: [] };
    let quietFor: number | null = null;
    if (log) {
      try {
        p = logProgress(fs.readFileSync(log, "utf8"));
        quietFor = Math.max(0, now - Math.floor(fs.statSync(log).mtimeMs / 1000));
      } catch {
        // the log went away between the lookup and the read
      }
    }
    return { ...p, id: j.id, elapsed: jobElapsed(j, now), quietFor };
  });
}
