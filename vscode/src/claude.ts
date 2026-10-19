/** Text for the Claude Code terminal. No vscode import, so it can be tested. */

export interface AskItem { id: number; kind: string; project: string; ref: string; title: string }

/** One line, so typing it into a running claude session does not submit early. */
export function askPrompt(i: AskItem): string {
  const title = (i.title || i.ref).replace(/\s+/g, " ").trim();
  return `Look at lean brain item #${i.id} (${i.kind}, project ${i.project}, ref ${i.ref}): ${title}. ` +
    `Read it with brain show ${i.id}, then tell me what you find before changing anything.`;
}

/** The shell line that starts claude, with an optional first prompt in single quotes. */
export function claudeLine(command: string, prompt?: string): string {
  return prompt === undefined ? command : `${command} '${prompt.replace(/'/g, `'\\''`)}'`;
}
