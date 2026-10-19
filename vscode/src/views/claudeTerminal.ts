import * as vscode from "vscode";
import { askPrompt, type AskItem } from "../claude";

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
    this.term = vscode.window.createTerminal({
      name: NAME, shellPath: this.command(), shellArgs: prompt === undefined ? [] : [prompt], cwd: this.cwd(),
      iconPath: new vscode.ThemeIcon("sparkle"), location: { viewColumn: vscode.ViewColumn.Beside, preserveFocus: false },
    });
    this.term.show();
  }

  open(): void {
    const t = this.live();
    if (t) t.show();
    else this.start();
  }

  /** A session this extension started gets the prompt typed in without Enter, to read and edit first. Otherwise a new one starts with it. */
  ask(item: AskItem): void {
    const t = this.live();
    if (!t) return this.start(askPrompt(item));
    t.sendText(askPrompt(item), false);
    t.show();
  }
}
