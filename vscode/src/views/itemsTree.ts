import * as vscode from "vscode";
import type { Database } from "sql.js";
import * as db from "../db";
import { kindLook, toneColorId, type Look } from "../style";
import type { ItemRow } from "../types";

export function themeIcon(look: Look): vscode.ThemeIcon {
  const id = toneColorId(look.tone);
  return new vscode.ThemeIcon(look.icon, id ? new vscode.ThemeColor(id) : undefined);
}

type Node =
  | { t: "project"; project: string; items: ItemRow[] }
  | { t: "kind"; project: string; kind: string; items: ItemRow[] }
  | { t: "item"; item: ItemRow };

export class ItemsTree implements vscode.TreeDataProvider<Node> {
  private emitter = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.emitter.event;
  private items: ItemRow[] = [];

  setData(database: Database | undefined): void {
    this.items = database ? db.openItems(database) : [];
    this.emitter.fire();
  }

  getChildren(node?: Node): Node[] {
    if (!node) {
      const by = new Map<string, ItemRow[]>();
      for (const i of this.items) by.set(i.project, [...(by.get(i.project) ?? []), i]);
      return [...by].map(([project, items]) => ({ t: "project", project, items }));
    }
    if (node.t === "project") {
      const by = new Map<string, ItemRow[]>();
      for (const i of node.items) by.set(i.kind, [...(by.get(i.kind) ?? []), i]);
      return [...by].map(([kind, items]) => ({ t: "kind", project: node.project, kind, items }));
    }
    if (node.t === "kind") return node.items.map((item) => ({ t: "item", item }));
    return [];
  }

  getTreeItem(node: Node): vscode.TreeItem {
    if (node.t === "project" || node.t === "kind") {
      const label = node.t === "project" ? node.project : node.kind;
      const ti = new vscode.TreeItem(`${label} (${node.items.length})`, node.t === "project"
        ? vscode.TreeItemCollapsibleState.Expanded : vscode.TreeItemCollapsibleState.Collapsed);
      ti.contextValue = node.t;
      ti.iconPath = node.t === "project" ? new vscode.ThemeIcon("repo", new vscode.ThemeColor("charts.blue")) : themeIcon(kindLook(node.kind));
      return ti;
    }
    const i = node.item;
    const now = Math.floor(Date.now() / 1000);
    const ti = new vscode.TreeItem(i.title || i.ref, vscode.TreeItemCollapsibleState.None);
    ti.description = [i.ref !== i.title ? i.ref : "", i.severity, i.cycle, `x${i.occurrences}`, db.relativeTime(i.last_seen, now)]
      .filter(Boolean).join(" | ");
    ti.tooltip = `#${i.id} ${i.kind} ${i.project}\n${i.title}`;
    ti.contextValue = "item";
    ti.iconPath = themeIcon(kindLook(i.kind));
    ti.command = { command: "leanBrain.showItem", title: "Show item", arguments: [i.id] };
    ti.id = `item-${i.id}`;
    return ti;
  }
}

export function itemFromArg(arg: unknown): { id: number; ref: string; title: string } | undefined {
  const n = arg as { t?: string; item?: ItemRow } | undefined;
  if (n?.t === "item" && n.item) return { id: n.item.id, ref: n.item.ref, title: n.item.title || n.item.ref };
  return undefined;
}
