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
