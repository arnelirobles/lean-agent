import * as vscode from "vscode";
import type { Database } from "sql.js";
import * as db from "../db";
import type { JobRow, QueueData } from "../types";

type Node = { t: "group"; label: string; jobs: JobRow[] } | { t: "job"; job: JobRow };

export class QueueTree implements vscode.TreeDataProvider<Node> {
  private emitter = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.emitter.event;
  private q: QueueData = { running: [], queued: [], finished: [] };

  setData(database: Database | undefined): void {
    this.q = database ? db.queue(database) : { running: [], queued: [], finished: [] };
    this.emitter.fire();
  }

  getChildren(node?: Node): Node[] {
    if (!node) {
      const groups: Node[] = [
        { t: "group", label: "Running", jobs: this.q.running },
        { t: "group", label: "Queued", jobs: this.q.queued },
        { t: "group", label: "Last finished", jobs: this.q.finished },
      ];
      return groups;
    }
    return node.t === "group" ? node.jobs.map((job) => ({ t: "job", job })) : [];
  }

  getTreeItem(node: Node): vscode.TreeItem {
    if (node.t === "group") {
      return new vscode.TreeItem(`${node.label} (${node.jobs.length})`,
        node.jobs.length ? vscode.TreeItemCollapsibleState.Expanded : vscode.TreeItemCollapsibleState.None);
    }
    const j = node.job;
    const now = Math.floor(Date.now() / 1000);
    const el = db.jobElapsed(j, now);
    const ti = new vscode.TreeItem(`#${j.id} ${db.shortTestName(j.test_project)}`, vscode.TreeItemCollapsibleState.None);
    const parts = [j.status === "queued" ? "" : j.status, j.cycle, j.project];
    if (el != null) parts.push(db.formatDuration(el));
    else if (j.status === "queued") parts.push("waiting " + db.relativeTime(j.enqueued_at, now).replace(" ago", ""));
    ti.description = parts.filter(Boolean).join(" | ");
    ti.tooltip = j.reason ? `${j.status}: ${j.reason}` : j.status;
    return ti;
  }
}
