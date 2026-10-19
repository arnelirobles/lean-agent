import * as vscode from "vscode";
import { askPrompt, claudeLine, type AskItem } from "../claude";

const NAME = "Claude Code";

/** One Claude Code terminal in an editor tab beside the dashboard. */
export class ClaudeTerminal {
  constructor(private readonly command: () => string, private readonly cwd: () => string | undefined) {}

  private find(): vscode.Terminal | undefined {
    return vscode.window.terminals.find((t) => t.name === NAME && t.exitStatus === undefined);
  }

  private start(prompt?: string): void {
    const t = vscode.window.createTerminal({
      name: NAME, cwd: this.cwd(), iconPath: new vscode.ThemeIcon("sparkle"),
      location: { viewColumn: vscode.ViewColumn.Beside, preserveFocus: false },
    });
    t.sendText(claudeLine(this.command(), prompt));
    t.show();
  }

  open(): void {
    const t = this.find();
    if (t) t.show();
    else this.start();
  }

  /** A running session gets the prompt typed in without Enter, so it can be read and edited first. A new one starts with it. */
  ask(item: AskItem): void {
    const t = this.find();
    if (!t) return this.start(askPrompt(item));
    t.sendText(askPrompt(item), false);
    t.show();
  }
}
