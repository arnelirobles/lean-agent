import * as fs from "node:fs";
import * as vscode from "vscode";
import { askPrompt, resolveClaude, workPrompt, type AskItem } from "../claude";

const isExec = (p: string) => {
  try { fs.accessSync(p, fs.constants.X_OK); return fs.statSync(p).isFile(); } catch { return false; }
};

const NAME = "Claude Code";

/**
 * One Claude Code terminal in an editor tab beside the dashboard. claude is the terminal's own process (shellPath),
 * not a command typed into a shell, so the prompt is a plain argument and nothing typed later can reach a shell.
 * When claude exits the terminal closes with it.
 */
export class ClaudeTerminal {
  private term?: vscode.Terminal;

  constructor(private readonly command: () => string, private readonly cwd: () => string | undefined) {
    vscode.window.onDidCloseTerminal((t) => { if (t === this.term) this.term = undefined; });
  }

  private live(): vscode.Terminal | undefined {
    return this.term && this.term.exitStatus === undefined ? this.term : undefined;
  }

  private start(prompt?: string): void {
    const exe = resolveClaude(this.command(), process.env.PATH ?? "", isExec);
    if (!exe) {
      void vscode.window.showErrorMessage("Lean Brain: claude not found. Set leanBrain.claudeCommand to the absolute path of the claude executable.");
      return;
    }
    this.term = vscode.window.createTerminal({
      name: NAME, shellPath: exe, shellArgs: prompt === undefined ? [] : [prompt], cwd: this.cwd(),
      iconPath: new vscode.ThemeIcon("sparkle"), location: { viewColumn: vscode.ViewColumn.Beside, preserveFocus: false },
    });
    this.term.show();
  }

  open(): void {
    const t = this.live();
    if (t) t.show();
    else this.start();
  }

  /** Always a new session, so the task gets a clean context and does not land in another conversation. */
  send(item: AskItem): void {
    this.start(workPrompt(item));
  }

  /** A session this extension started gets the prompt typed in without Enter, to read and edit first. Otherwise a new one starts with it. */
  ask(item: AskItem): void {
    const t = this.live();
    if (!t) return this.start(askPrompt(item));
    t.sendText(askPrompt(item), false);
    t.show();
  }
}
