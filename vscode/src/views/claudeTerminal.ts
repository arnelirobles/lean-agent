import { randomUUID } from "node:crypto";
import * as fs from "node:fs";
import * as vscode from "vscode";
import { askPrompt, claudeArgs, resolveClaude, workPrompt, type AskItem } from "../claude";

const isExec = (p: string) => {
  try { fs.accessSync(p, fs.constants.X_OK); return fs.statSync(p).isFile(); } catch { return false; }
};

const NAME = "Claude Code";

/**
 * Claude Code terminals in editor tabs beside the dashboard. claude is the terminal's own process (shellPath), not a
 * command typed into a shell, so the prompt is a plain argument and nothing typed later can reach a shell. When claude
 * exits the terminal closes with it. Every session started here gets a known id, so an answer to a question it files
 * can be typed into the same terminal instead of starting a second copy.
 */
export class ClaudeTerminal {
  private last?: vscode.Terminal;
  private bySession = new Map<string, vscode.Terminal>();

  constructor(private readonly command: () => string, private readonly cwd: () => string | undefined) {
    vscode.window.onDidCloseTerminal((t) => {
      if (t === this.last) this.last = undefined;
      for (const [id, term] of this.bySession) if (term === t) this.bySession.delete(id);
    });
  }

  private static alive(t?: vscode.Terminal): vscode.Terminal | undefined {
    return t && t.exitStatus === undefined ? t : undefined;
  }

  /** sessionId is the id the session runs under, when known: a new one, or a resume that is not a fork. */
  private start(args: string[], sessionId?: string, cwd?: string): void {
    const exe = resolveClaude(this.command(), process.env.PATH ?? "", isExec);
    if (!exe) {
      void vscode.window.showErrorMessage("Lean Brain: claude not found. Set leanBrain.claudeCommand to the absolute path of the claude executable.");
      return;
    }
    const term = vscode.window.createTerminal({
      name: NAME, shellPath: exe, shellArgs: args, cwd: cwd ?? this.cwd(),
      iconPath: new vscode.ThemeIcon("sparkle"), location: { viewColumn: vscode.ViewColumn.Beside, preserveFocus: false },
    });
    this.last = term;
    if (sessionId) this.bySession.set(sessionId, term);
    term.show();
  }

  private fresh(prompt?: string): void {
    const id = randomUUID();
    this.start(claudeArgs({ sessionId: id }, prompt), id);
  }

  open(): void {
    const t = ClaudeTerminal.alive(this.last);
    if (t) t.show();
    else this.fresh();
  }

  /** Always a new session, so the task gets a clean context and does not land in another conversation. */
  send(item: AskItem): void {
    this.fresh(workPrompt(item));
  }

  /** A session this extension started gets the prompt typed in without Enter, to read and edit first. Otherwise a new one starts with it. */
  ask(item: AskItem): void {
    const t = ClaudeTerminal.alive(this.last);
    if (!t) return this.fresh(askPrompt(item));
    t.sendText(askPrompt(item), false);
    t.show();
  }

  /** True when a terminal started here still runs that session. */
  owns(sessionId: string): boolean {
    return !!ClaudeTerminal.alive(this.bySession.get(sessionId));
  }

  /** Types the answer into the session's own terminal and submits it. */
  typeInto(sessionId: string, prompt: string): boolean {
    const t = ClaudeTerminal.alive(this.bySession.get(sessionId));
    if (!t) return false;
    t.sendText(prompt, true);
    t.show();
    return true;
  }

  /** claude --resume in the session's folder. A fork gets a new id from claude, so it is not tracked by the old one. */
  resume(sessionId: string, cwd: string, prompt: string, fork: boolean): void {
    this.start(claudeArgs({ resume: sessionId, fork }, prompt), fork ? undefined : sessionId, cwd);
  }
}
