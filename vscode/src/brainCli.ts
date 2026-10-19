import { execFile } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

// Runs bin/brain with argument arrays. No shell. This is the only way the extension changes anything.

export function defaultDbPath(setting: string, env: NodeJS.ProcessEnv = process.env, home = os.homedir()): string {
  return setting || env.LEAN_BRAIN_DB || path.join(home, ".local", "share", "lean", "brain.db");
}

function isExecutable(p: string): boolean {
  try {
    fs.accessSync(p, fs.constants.X_OK);
    return fs.statSync(p).isFile();
  } catch {
    return false;
  }
}

function compareVersions(a: string, b: string): number {
  const pa = a.split(/[.\-+]/), pb = b.split(/[.\-+]/);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = pa[i] ?? "0", y = pb[i] ?? "0";
    const nx = Number(x), ny = Number(y);
    const d = !Number.isNaN(nx) && !Number.isNaN(ny) ? nx - ny : x.localeCompare(y);
    if (d) return d;
  }
  return 0;
}

/** brainPath setting, else `brain` on PATH, else the newest lean-agent plugin in the Claude plugin cache. */
export function findBrain(setting: string, env: NodeJS.ProcessEnv = process.env, home = os.homedir()): string | undefined {
  if (setting) return isExecutable(setting) ? setting : undefined;
  for (const dir of (env.PATH ?? "").split(path.delimiter)) {
    if (!dir) continue;
    const p = path.join(dir, "brain");
    if (isExecutable(p)) return p;
  }
  const root = path.join(home, ".claude", "plugins", "cache", "lean-agent", "lean-agent");
  let versions: string[] = [];
  try {
    versions = fs.readdirSync(root);
  } catch {
    return undefined;
  }
  versions.sort(compareVersions);
  for (const v of versions.reverse()) {
    const p = path.join(root, v, "bin", "brain");
    if (isExecutable(p)) return p;
  }
  return undefined;
}

export interface CliResult { ok: boolean; stdout: string; stderr: string }

export function runBrain(brain: string, args: string[], dbPath: string, cwd?: string): Promise<CliResult> {
  return new Promise((resolve) => {
    execFile(brain, args, { env: { ...process.env, LEAN_BRAIN_DB: dbPath }, cwd, timeout: 30000, maxBuffer: 4 * 1024 * 1024 },
      (err, stdout, stderr) => resolve({ ok: !err, stdout: String(stdout), stderr: String(stderr || err?.message || "") }));
  });
}

export function closeArgs(id: number, resolution: "fixed" | "not-a-bug"): string[] {
  return resolution === "fixed"
    ? ["close", String(id), "--resolution", "fixed"]
    : ["close", String(id), "--wontfix", "--resolution", "not-a-bug"];
}

/** Close a question with the answer as the note. delivered: the answer already reached the session that asked. */
export function answerArgs(id: number, answer: string, delivered: boolean): string[] {
  return ["close", String(id), "--resolution", "answered", "--note", answer, ...(delivered ? ["--delivered"] : [])];
}

export function reopenArgs(id: number): string[] {
  return ["reopen", String(id)];
}

export function resumeArgs(project?: string): string[] {
  return project ? ["resume", "--project", project] : ["resume"];
}

/** Swap a queued job with the one before or after it in run order. */
export function moveArgs(id: number, where: "up" | "down"): string[] {
  return ["queue", "move", String(id), where];
}

/** Whether this brain has `queue move`. A brain from before it lists no move in its queue help. */
export async function brainCanMove(brain: string): Promise<boolean> {
  const r = await new Promise<string>((resolve) =>
    execFile(brain, ["queue", "--help"], { timeout: 10000 }, (_err, stdout, stderr) => resolve(`${stdout}${stderr}`)));
  return /[{,]move[,}]/.test(r);
}

/** Which lanes may claim a queued job: remote only, this machine only, or any. */
export function routeArgs(id: number, to: "remote" | "local" | "any"): string[] {
  return ["queue", "route", String(id), "--to", to];
}
