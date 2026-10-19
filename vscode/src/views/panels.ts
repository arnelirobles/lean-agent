import * as vscode from "vscode";
import type { Database } from "sql.js";
import * as db from "../db";
import { dashboardHtml } from "../dashboard/dashboardHtml";
import { itemHtml } from "../dashboard/itemHtml";
import { makeNonce } from "../dashboard/page";
import * as os from "os";
import { readRemotes } from "../lanes";
import { liveJobs, testPhaseDir } from "../progress";
import type { DashboardData, JobRow } from "../types";

/** The codicon stylesheet copied to out/codicons by the build, as a webview URI. */
function codicons(panel: vscode.WebviewPanel, root: vscode.Uri): string {
  return panel.webview.asWebviewUri(vscode.Uri.joinPath(root, "out", "codicons", "codicon.css")).toString();
}

/** The media folder (fonts, fox mark) as a webview URI, without a trailing slash. */
function media(panel: vscode.WebviewPanel, root: vscode.Uri): string {
  return panel.webview.asWebviewUri(vscode.Uri.joinPath(root, "media")).toString().replace(/\/$/, "");
}

function options(root: vscode.Uri): vscode.WebviewPanelOptions & vscode.WebviewOptions {
  return { enableScripts: true, localResourceRoots: [vscode.Uri.joinPath(root, "out", "codicons"), vscode.Uri.joinPath(root, "media")] };
}

interface Msg { type: string; text?: string; id?: number }

/** What the webviews can ask the extension to do. */
export interface Actions {
  openItem: (id: number) => void; refresh: () => void; claude: () => void; ask: (id: number) => void; send: (id: number) => void;
  answer: (id: number, text: string) => void; route: (id: number, to: "remote" | "local" | "any") => void;
  move: (id: number, where: "up" | "down") => void;
}

function wire(panel: vscode.WebviewPanel, a: Actions): void {
  panel.webview.onDidReceiveMessage((m: Msg) => {
    if (m.type === "copy" && typeof m.text === "string") {
      void vscode.env.clipboard.writeText(m.text);
      void vscode.window.setStatusBarMessage("Copied to clipboard", 2000);
    } else if (m.type === "open" && typeof m.id === "number") a.openItem(m.id);
    else if (m.type === "ask" && typeof m.id === "number") a.ask(m.id);
    else if (m.type === "send" && typeof m.id === "number") a.send(m.id);
    else if (m.type === "answer" && typeof m.id === "number" && typeof m.text === "string" && m.text.trim() && m.text.length <= 20000)
      a.answer(m.id, m.text.trim());
    else if (m.type === "route" && typeof m.id === "number" && (m.text === "remote" || m.text === "local" || m.text === "any"))
      a.route(m.id, m.text);
    else if (m.type === "move" && typeof m.id === "number" && (m.text === "up" || m.text === "down"))
      a.move(m.id, m.text);
    else if (m.type === "claude") a.claude();
    else if (m.type === "refresh") a.refresh();
  });
}

function emptyData(): DashboardData {
  return {
    schemaVersion: null, waiting: [], headline: { realOpen: 0, flakyOpen: 0, closed7d: 0, runs7d: 0, testSeconds7d: 0 },
    cycles: [], suites: [], flakiest: [], load: [], stale: [], queue: { running: [], queued: [], finished: [], today: [] }, generatedAt: Math.floor(Date.now() / 1000),
  };
}

export class DashboardPanel {
  private panel?: vscode.WebviewPanel;
  private running: JobRow[] = [];
  private timer?: NodeJS.Timeout;
  /** Whether the brain the extension runs has `queue move`. */
  canMove = false;
  constructor(private readonly root: vscode.Uri, private readonly actions: Actions) {}

  show(database: Database | undefined, note: string): void {
    if (!this.panel) {
      this.panel = vscode.window.createWebviewPanel("leanBrain.dashboard", "Lean Brain", vscode.ViewColumn.Active, options(this.root));
      this.panel.onDidDispose(() => {
        this.panel = undefined;
        clearInterval(this.timer);
      });
      wire(this.panel, this.actions);
      // Running jobs' logs grow between database refreshes, so the live blocks get their own 3 second tick.
      this.timer = setInterval(() => this.postLive(), 3000);
    } else this.panel.reveal();
    this.render(database, note);
  }

  render(database: Database | undefined, note: string): void {
    if (!this.panel) return;
    const nonce = makeNonce();
    const data = database ? db.dashboard(database, Math.floor(Date.now() / 1000)) : emptyData();
    data.remotes = readRemotes(process.env, os.homedir());
    data.canMove = this.canMove;
    this.running = data.queue.running;
    this.panel.webview.html = dashboardHtml(data, nonce, this.panel.webview.cspSource, database ? note : note || "No data.",
      codicons(this.panel, this.root), media(this.panel, this.root));
    this.postLive();
  }

  private postLive(): void {
    if (!this.panel || this.running.length === 0) return;
    const jobs = liveJobs(this.running, testPhaseDir(process.env, os.homedir()), Math.floor(Date.now() / 1000));
    void this.panel.webview.postMessage({ type: "live", jobs });
  }
}

export class ItemPanel {
  private panel?: vscode.WebviewPanel;
  private current?: number;
  constructor(private readonly root: vscode.Uri, private readonly actions: Actions) {}

  show(database: Database | undefined, id: number): void {
    if (!database) return;
    this.current = id;
    if (!this.panel) {
      this.panel = vscode.window.createWebviewPanel("leanBrain.item", `Item ${id}`, vscode.ViewColumn.Beside, options(this.root));
      this.panel.onDidDispose(() => (this.panel = undefined));
      wire(this.panel, this.actions);
    } else this.panel.reveal(undefined, true);
    this.render(database);
  }

  render(database: Database | undefined): void {
    if (!this.panel || !database || this.current === undefined) return;
    const item = db.getItem(database, this.current);
    this.panel.title = `Item ${this.current}`;
    const nonce = makeNonce();
    this.panel.webview.html = item
      ? itemHtml(item, Math.floor(Date.now() / 1000), nonce, this.panel.webview.cspSource, codicons(this.panel, this.root), media(this.panel, this.root))
      : `<p>Item ${this.current} is gone.</p>`;
  }
}
