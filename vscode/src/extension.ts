import * as fs from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";
import * as cli from "./brainCli";
import { answerPrompt, answerRoute, canSend, validSession } from "./claude";
import * as db from "./db";
import { Store } from "./store";
import { projectNames } from "./workspace";
import { ItemsTree, itemFromArg } from "./views/itemsTree";
import { ClaudeTerminal } from "./views/claudeTerminal";
import { DashboardPanel, ItemPanel, type Actions } from "./views/panels";
import { QueueTree } from "./views/queueTree";
import { StatusBar } from "./views/statusBar";

export async function activate(ctx: vscode.ExtensionContext): Promise<void> {
  const cfg = () => vscode.workspace.getConfiguration("leanBrain");
  // Only the user-level value counts, so a workspace cannot point these at another program.
  const userSetting = (key: string) => cfg().inspect<string>(key)?.globalValue ?? "";
  const dbPath = () => cli.defaultDbPath(userSetting("dbPath"));
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
  const claude = new ClaudeTerminal(() => userSetting("claudeCommand"), () => folder);
  const ask = (id: number) => {
    const it = store.database && db.getItem(store.database, id);
    if (it) claude.ask(it);
  };
  const send = (id: number) => {
    const it = store.database && db.getItem(store.database, id);
    if (it && canSend(it.kind)) claude.send(it);
  };
  const answer = async (id: number, text: string): Promise<void> => {
    const d = store.database;
    const it = d && db.getItem(d, id);
    if (!d || !it || it.status !== "open") return;
    const sid = it.session_id ?? "";
    const route = answerRoute(sid, validSession(sid) ? db.sessionState(d, sid) : "unknown", validSession(sid) && claude.owns(sid));
    let how: "type" | "resume" | "fork" | "later" | "save" = route === "ask" ? "later" : route;
    if (route === "ask") {
      const later = "On its next prompt", fork = "Resume as a fork";
      const pick = await vscode.window.showWarningMessage(`The session that asked #${id} may still be open in another window.`, {
        modal: true,
        detail: "Resuming it would run two copies of one conversation. On its next prompt: the answer is saved and that session " +
          "gets it the next time you type in it. Resume as a fork: a new session starts from that conversation with the answer, " +
          "and the old one stays as it was.",
      }, later, fork);
      if (!pick) return;
      how = pick === fork ? "fork" : "later";
    }
    const dir = sessionDir(it.session_cwd);
    if ((how === "resume" || how === "fork") && !dir) {
      void vscode.window.showErrorMessage(`Lean Brain: the folder of the session that asked (${it.session_cwd || "none"}) is gone. Nothing was sent.`);
      return;
    }
    const brain = findOrWarn();
    if (!brain) return;
    const delivered = how === "type" || how === "resume" || how === "fork";
    const r = await cli.runBrain(brain, cli.answerArgs(id, text, delivered), dbPath(), folder);
    if (!r.ok) {
      void vscode.window.showErrorMessage(`brain: ${r.stderr.trim()}`);
      return;
    }
    const prompt = answerPrompt(it, text);
    if (how === "type") claude.typeInto(sid, prompt);
    else if (how === "resume" || how === "fork") claude.resume(sid, dir!, prompt, how === "fork");
    else void vscode.window.showInformationMessage(how === "later"
      ? `Answer saved. The session that asked #${id} gets it with your next prompt there.`
      : `Answer saved on #${id}.`);
    await refresh();
  };
  const actions: Actions = {
    openItem: (id) => itemPanel.show(store.database, id), refresh: () => void refresh(), claude: () => claude.open(), ask, send,
    answer: (id, text) => void answer(id, text), route: (id, to) => void routeJob(id, to),
  };
  const dashboard = new DashboardPanel(ctx.extensionUri, actions);
  const itemPanel = new ItemPanel(ctx.extensionUri, actions);

  ctx.subscriptions.push(
    status,
    vscode.window.registerTreeDataProvider("leanBrain.items", items),
    vscode.window.registerTreeDataProvider("leanBrain.queue", queueTree),
    vscode.commands.registerCommand("leanBrain.openDashboard", async () => {
      await refresh();
      dashboard.show(store.database, store.note);
    }),
    vscode.commands.registerCommand("leanBrain.refresh", () => refresh()),
    vscode.commands.registerCommand("leanBrain.openClaude", () => claude.open()),
    vscode.commands.registerCommand("leanBrain.sendToClaude", (arg) => {
      const it = itemFromArg(arg);
      if (it) send(it.id);
    }),
    vscode.commands.registerCommand("leanBrain.askClaude", (arg) => {
      const it = itemFromArg(arg);
      if (it) ask(it.id);
    }),
    vscode.commands.registerCommand("leanBrain.showItem", (id: number) => itemPanel.show(store.database, id)),
    vscode.commands.registerCommand("leanBrain.copyRef", async (arg) => {
      const it = itemFromArg(arg);
      if (it) await vscode.env.clipboard.writeText(it.ref);
    }),
    vscode.commands.registerCommand("leanBrain.closeFixed", (arg) => change(arg, "close-fixed")),
    vscode.commands.registerCommand("leanBrain.closeNotABug", (arg) => change(arg, "close-notabug")),
    vscode.commands.registerCommand("leanBrain.reopen", (arg) => change(arg, "reopen")),
    vscode.commands.registerCommand("leanBrain.sendToRemote", (arg) => route(arg, "remote")),
    vscode.commands.registerCommand("leanBrain.keepLocal", (arg) => route(arg, "local")),
    vscode.commands.registerCommand("leanBrain.anyLane", (arg) => route(arg, "any")),
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

  /** The asking session's folder, only if it is an absolute path to a directory that still exists. */
  function sessionDir(dir: string | undefined): string | undefined {
    try {
      return dir && path.isAbsolute(dir) && fs.statSync(dir).isDirectory() ? dir : undefined;
    } catch {
      return undefined;
    }
  }

  function findOrWarn(): string | undefined {
    const brain = cli.findBrain(userSetting("brainPath"));
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

  /** Set which lanes may run a queued job. Reversible, so no confirmation; brain's warning shows as a warning. */
  async function route(arg: unknown, to: "remote" | "local" | "any"): Promise<void> {
    const id = (arg as { t?: string; job?: { id?: unknown } } | undefined)?.job?.id;
    if (typeof id === "number") await routeJob(id, to);
  }

  async function routeJob(id: number, to: "remote" | "local" | "any"): Promise<void> {
    const brain = findOrWarn();
    if (!brain) return;
    const r = await cli.runBrain(brain, cli.routeArgs(id, to), dbPath(), folder);
    if (!r.ok) void vscode.window.showErrorMessage(`brain: ${r.stderr.trim()}`);
    else if (r.stderr.trim()) void vscode.window.showWarningMessage(r.stderr.trim());
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
