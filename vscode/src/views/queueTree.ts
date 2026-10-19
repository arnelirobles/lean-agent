import * as vscode from "vscode";
import type { Database } from "sql.js";
import * as db from "../db";
import { jobLook } from "../style";
import type { JobRow, QueueData } from "../types";
import { themeIcon } from "./itemsTree";

type Node = { t: "group"; label: string; jobs: JobRow[] } | { t: "job"; job: JobRow };

export class QueueTree implements vscode.TreeDataProvider<Node> {
  private emitter = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.emitter.event;
  private q: QueueData = { running: [], queued: [], finished: [], today: [] };

  setData(database: Database | undefined): void {
    this.q = database ? db.queue(database) : { running: [], queued: [], finished: [], today: [] };
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
      const g = new vscode.TreeItem(`${node.label} (${node.jobs.length})`,
        node.jobs.length ? vscode.TreeItemCollapsibleState.Expanded : vscode.TreeItemCollapsibleState.None);
      g.iconPath = themeIcon(node.label === "Running" ? jobLook("running") : node.label === "Queued" ? { icon: "list-ordered", tone: "yellow" }
        : { icon: "history", tone: "green" });
      return g;
    }
    const j = node.job;
    const now = Math.floor(Date.now() / 1000);
    const el = db.jobElapsed(j, now);
    const ti = new vscode.TreeItem(`#${j.id} ${db.shortTestName(j.test_project)}`, vscode.TreeItemCollapsibleState.None);
    const parts = [j.status === "queued" ? "" : j.status, j.cycle, j.project, db.laneLabel(j),
      j.status === "running" && j.host ? `on ${j.host}` : ""];
    if (el != null) parts.push(db.formatDuration(el));
    else if (j.status === "queued") parts.push("waiting " + db.relativeTime(j.enqueued_at, now).replace(" ago", ""));
    ti.description = parts.filter(Boolean).join(" | ");
    ti.tooltip = j.reason ? `${j.status}: ${j.reason}` : j.status;
    ti.iconPath = themeIcon(jobLook(j.status));
    ti.contextValue = db.jobContext(j);
    return ti;
  }
}
