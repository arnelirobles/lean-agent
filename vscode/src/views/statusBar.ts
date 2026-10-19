import * as vscode from "vscode";
import * as db from "../db";
import type { Database } from "sql.js";

export class StatusBar {
  private item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  private timer?: NodeJS.Timeout;
  constructor() {
    this.item.command = "leanBrain.openDashboard";
    this.item.name = "Lean Brain";
    this.item.text = "brain: ...";
    this.item.show();
  }

  update(database: Database | undefined, projects: string[], note: string): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = undefined;
    if (!database) {
      this.item.text = "$(database) brain: none";
      this.item.tooltip = note || "No brain database";
      return;
    }
    const render = () => {
      const now = Math.floor(Date.now() / 1000);
      const { counts, scoped } = db.openCounts(database, projects);
      const running = db.queue(database).running[0];
      let text = `$(${counts.real ? "bug" : "pass"}) ${counts.real}  $(warning) ${counts.flaky}`;
      if (running) {
        const el = db.jobElapsed(running, now);
        text += `  $(sync~spin) ${db.shortTestName(running.test_project)}${el == null ? "" : " " + db.formatDuration(el)}`;
      }
      this.item.text = text;
      this.item.color = new vscode.ThemeColor(counts.real ? "charts.red" : "charts.green");
      this.item.tooltip = `Brain: ${counts.real} real, ${counts.flaky} flaky. ` + (scoped ? `Open items for ${projects.join(" / ")}` : "Open items, all projects") + (note ? `\n${note}` : "");
    };
    render();
    if (db.queue(database).running.length) this.timer = setInterval(render, 5000);
  }

  dispose(): void {
    if (this.timer) clearInterval(this.timer);
    this.item.dispose();
  }
}
