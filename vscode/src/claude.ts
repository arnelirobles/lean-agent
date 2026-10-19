/** Text for the Claude Code terminal. No vscode import, so it can be tested. */

export interface AskItem { id: number; kind: string; project: string; ref: string; title: string }

/** Brain fields come from agents and test output, so control characters and line breaks become spaces. */
export function clean(s: unknown): string {
  return String(s ?? "").replace(/[\x00-\x1f\x7f-\x9f\u2028\u2029]+/g, " ").replace(/\s+/g, " ").trim();
}

/** One line with no control characters, so typing it into claude never submits early or reaches a shell. */
export function askPrompt(i: AskItem): string {
  const id = Number.isInteger(i.id) ? i.id : 0;
  return `Look at lean brain item #${id} (${clean(i.kind)}, project ${clean(i.project)}, ref ${clean(i.ref)}): ` +
    `${clean(i.title || i.ref)}. Read it with brain show ${id}, then tell me what you find before changing anything.`;
}

/** Kinds Claude can take on by itself: pending work, risks and questions. */
export function canSend(kind: string): boolean {
  return /pending|risk|question/i.test(kind);
}

/** A task for a fresh session. Questions get a recommendation and stay open, because the answer is the user's call. */
export function workPrompt(i: AskItem): string {
  const id = Number.isInteger(i.id) ? i.id : 0;
  const head = `Work on lean brain item #${id} (${clean(i.kind)}, project ${clean(i.project)}, ref ${clean(i.ref)}): ` +
    `${clean(i.title || i.ref)}. Read it first with brain show ${id}. `;
  return /question/i.test(i.kind)
    ? head + "Work out the answer from the code and the docs, then recommend one with your reasons. Do not close the item, I decide."
    : head + `Fix it, verify the fix, then close it with brain close ${id} --resolution fixed --note followed by one line on what you did.`;
}

/**
 * The claude executable as an absolute path, or undefined. A bare name is looked up only in absolute PATH entries,
 * never the workspace, so a planted ./claude cannot run. Relative paths and Windows .cmd/.bat shims are refused,
 * because cmd.exe re-parses their arguments.
 */
export function resolveClaude(setting: string, pathEnv: string, isExec: (p: string) => boolean,
  platform: string = process.platform, sep = platform === "win32" ? ";" : ":"): string | undefined {
  const name = setting.trim() || "claude";
  const shim = (p: string) => platform === "win32" && /\.(cmd|bat)$/i.test(p);
  const abs = (p: string) => p.startsWith("/") || /^[A-Za-z]:[\\/]/.test(p);
  if (/[\\/]/.test(name)) return abs(name) && !shim(name) && isExec(name) ? name : undefined;
  const exts = platform === "win32" ? [".exe", ""] : [""];
  for (const dir of pathEnv.split(sep)) {
    if (!abs(dir)) continue;
    for (const ext of exts) {
      const p = `${dir.replace(/[\\/]+$/, "")}${platform === "win32" ? "\\" : "/"}${name}${ext}`;
      if (!shim(p) && isExec(p)) return p;
    }
  }
  return undefined;
}
