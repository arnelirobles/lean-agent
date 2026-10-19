import * as fs from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";
import * as cli from "./brainCli";
import { Store } from "./store";
import { projectNames } from "./workspace";
import { ItemsTree, itemFromArg } from "./views/itemsTree";
import { DashboardPanel, ItemPanel } from "./views/panels";
import { QueueTree } from "./views/queueTree";
import { StatusBar } from "./views/statusBar";

export async function activate(ctx: vscode.ExtensionContext): Promise<void> {
  const cfg = () => vscode.workspace.getConfiguration("leanBrain");
  const dbPath = () => cli.defaultDbPath(cfg().get<string>("dbPath", ""));
  const store = new Store(path.join(ctx.extensionPath, "out", "sql-wasm.wasm"));
  const items = new ItemsTree();
  const queueTree = new QueueTree();
  const status = new StatusBar();
  let projects: string[] = [];
  const folder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  if (folder) projects = await projectNames(folder);

  const refresh = async (): Promise<void> => {
    await store.refresh(dbPath());
    const d = store.database;
    status.update(d, projects, store.note);
    items.setData(d);
    queueTree.setData(d);
    dashboard.render(d, store.note);
    itemPanel.render(d);
  };
  const dashboard = new DashboardPanel((id) => itemPanel.show(store.database, id), () => void refresh());
  const itemPanel = new ItemPanel(() => void refresh());

  ctx.subscriptions.push(
    status,
    vscode.window.registerTreeDataProvider("leanBrain.items", items),
    vscode.window.registerTreeDataProvider("leanBrain.queue", queueTree),
    vscode.commands.registerCommand("leanBrain.openDashboard", async () => {
      await refresh();
      dashboard.show(store.database, store.note);
    }),
    vscode.commands.registerCommand("leanBrain.refresh", () => refresh()),
    vscode.commands.registerCommand("leanBrain.showItem", (id: number) => itemPanel.show(store.database, id)),
    vscode.commands.registerCommand("leanBrain.copyRef", async (arg) => {
      const it = itemFromArg(arg);
      if (it) await vscode.env.clipboard.writeText(it.ref);
    }),
    vscode.commands.registerCommand("leanBrain.closeFixed", (arg) => change(arg, "close-fixed")),
    vscode.commands.registerCommand("leanBrain.closeNotABug", (arg) => change(arg, "close-notabug")),
    vscode.commands.registerCommand("leanBrain.reopen", (arg) => change(arg, "reopen")),
    vscode.commands.registerCommand("leanBrain.showResume", async () => {
      const brain = findOrWarn();
      if (!brain) return;
      const out = vscode.window.createOutputChannel("Lean Brain");
      const r = await cli.runBrain(brain, cli.resumeArgs(projects[0]), dbPath(), folder);
      out.clear();
      out.appendLine(r.ok ? r.stdout.trim() || "Nothing open for this project." : `brain failed: ${r.stderr}`);
      out.show(true);
    }),
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("leanBrain")) {
        watch();
        void refresh();
      }
    }),
  );

  function findOrWarn(): string | undefined {
    const brain = cli.findBrain(cfg().get<string>("brainPath", ""));
    if (!brain) void vscode.window.showWarningMessage("Lean Brain: bin/brain not found. Set leanBrain.brainPath.");
    return brain;
  }

  async function change(arg: unknown, action: "close-fixed" | "close-notabug" | "reopen"): Promise<void> {
    const it = itemFromArg(arg);
    if (!it) return;
    const verb = action === "reopen" ? "Reopen" : action === "close-fixed" ? "Close as fixed" : "Close as not a bug";
    const pick = await vscode.window.showWarningMessage(`${verb}: #${it.id} ${it.title}?`, { modal: true }, verb);
    if (pick !== verb) return;
    const brain = findOrWarn();
    if (!brain) return;
    const args = action === "reopen" ? cli.reopenArgs(it.id) : cli.closeArgs(it.id, action === "close-fixed" ? "fixed" : "not-a-bug");
    const r = await cli.runBrain(brain, args, dbPath(), folder);
    if (!r.ok) void vscode.window.showErrorMessage(`brain: ${r.stderr.trim()}`);
    await refresh();
  }

  // Watch brain.db and its -wal, debounced to 2 s.
  let watchers: fs.FSWatcher[] = [];
  let debounce: NodeJS.Timeout | undefined;
  function watch(): void {
    for (const w of watchers) w.close();
    watchers = [];
    const file = dbPath();
    const dir = path.dirname(file);
    const names = new Set([path.basename(file), path.basename(file) + "-wal"]);
    try {
      watchers.push(fs.watch(dir, (_ev, name) => {
        if (!name || !names.has(String(name))) return;
        if (debounce) clearTimeout(debounce);
        debounce = setTimeout(() => void refresh(), 2000);
      }));
    } catch {
      // The folder may not exist yet. The manual refresh still works.
    }
  }
  ctx.subscriptions.push({ dispose: () => { for (const w of watchers) w.close(); if (debounce) clearTimeout(debounce); } });

  await store.init();
  watch();
  await refresh();
}

export function deactivate(): void {}
