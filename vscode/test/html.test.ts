import { beforeAll, describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import * as db from "../src/db";
import { esc } from "../src/dashboard/escape";
import { dashboardBody, dashboardHtml, listCommand } from "../src/dashboard/dashboardHtml";
import { itemHtml } from "../src/dashboard/itemHtml";
import { emptyDb, NOW, seededDb } from "./fixture";
import type { DashboardData } from "../src/types";

let data: DashboardData;
beforeAll(async () => { data = db.dashboard(await seededDb(), NOW); });

describe("escape", () => {
  it("escapes the five characters", () => {
    expect(esc(`<a href="x">'&'</a>`)).toBe("&lt;a href=&quot;x&quot;&gt;&#39;&amp;&#39;&lt;/a&gt;");
    expect(esc(null)).toBe("");
  });
});

describe("dashboard html", () => {
  it("escapes a <script> title in the waiting section", () => {
    expect(data.waiting.some((w) => w.title.includes("<script>"))).toBe(true);
    const html = dashboardHtml(data, "N0NCE", "vscode-resource:");
    expect(html).not.toContain("<script>alert(1)");
    expect(html).toContain("&lt;script&gt;alert(1)&lt;/script&gt;");
  });

  it("uses a nonce and a strict CSP, no remote sources", () => {
    const html = dashboardHtml(data, "N0NCE", "vscode-resource:");
    expect(html).toContain("script-src &#39;nonce-N0NCE&#39;");
    expect(html).toContain(`<script nonce="N0NCE">`);
    expect(html).toContain("default-src &#39;none&#39;");
    expect(html).not.toMatch(/https?:\/\//);
  });

  it("puts Waiting on you first and lists every section", () => {
    const body = dashboardBody(data);
    const order = ["Waiting on you", "Numbers", "Cycles", "Slowest suites", "Flakiest tests", "Machine load", "Hygiene"].map((s) => body.indexOf(`<h2>${s}</h2>`));
    expect(order.every((i) => i >= 0)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order);
    expect(body).toContain("blocker");
  });

  it("draws sparklines, bars and the load chart with a stalled marker", () => {
    const body = dashboardBody(data);
    expect(body).toContain(`class="spark"`);
    expect(body).toContain(`class="bar"`);
    expect(body).toContain(`class="chart"`);
    expect(body).toContain("stalled</title>");
  });

  it("puts each section in a tab and shows the first tab without script", () => {
    const body = dashboardBody(data);
    for (const t of ["overview", "cycles", "suites", "flaky", "load", "hygiene"]) {
      expect(body).toContain(`data-tab="${t}"`);
      expect(body).toContain(`data-panel="${t}"`);
    }
    expect(body).toContain(`class="tab t-blue active" role="tab" data-tab="overview"`);
    expect(body).toContain(`class="panel active" data-panel="overview"`);
    expect(body.match(/class="panel active"/g)?.length).toBe(1);
  });

  it("uses no inline style attributes, which the CSP would block", () => {
    expect(dashboardHtml(data, "N", "vscode-resource:")).not.toMatch(/\sstyle="/);
  });

  it("links the codicon stylesheet and allows its font", () => {
    const html = dashboardHtml(data, "N", "vscode-resource:", "", "vscode-resource:/out/codicons/codicon.css");
    expect(html).toContain(`<link rel="stylesheet" href="vscode-resource:/out/codicons/codicon.css">`);
    expect(html).toContain("font-src vscode-resource:");
  });

  it("draws flat charts with no gradient fills", () => {
    expect(dashboardBody(data)).not.toMatch(/linearGradient|url\(#/);
  });

  it("loads the bundled fonts and fox mark from the media folder", () => {
    const html = dashboardHtml(data, "N", "vscode-resource:", "", "", "vscode-resource:/media");
    expect(html).toContain(`url("vscode-resource:/media/fonts/plus-jakarta-sans.woff2")`);
    expect(html).toContain(`src="vscode-resource:/media/fox-mark.webp"`);
    expect(html).not.toContain("MEDIA");
  });

  it("offers Claude Code from the header", () => {
    expect(dashboardBody(data)).toContain(`data-claude="1"`);
  });

  it("hygiene button copies a read-only list command", () => {
    expect(listCommand("test-fail")).toBe("brain list --open --kind test-fail");
    expect(listCommand("a b")).toBe("brain list --open --kind 'a b'");
    const body = dashboardBody(data);
    expect(body).toContain(`data-copy="brain list --open --kind test-fail"`);
    expect(body).not.toMatch(/data-copy="[^"]*brain (close|reopen)/);
  });

  it("renders every section on an empty database", async () => {
    const empty = db.dashboard(await emptyDb(), NOW);
    const body = dashboardBody(empty, "a note");
    expect(body).toContain("Nothing is waiting on you.");
    expect(body).toContain("No runs recorded yet.");
    expect(body).toContain("No finished runs with a duration yet.");
    expect(body).toContain("No open flaky items.");
    expect(body).toContain("No load readings yet.");
    expect(body).toContain("No open items older than 14 days.");
    expect(body).toContain("a note");
  });
});

describe("item html", () => {
  it("escapes title and body and shows tags", async () => {
    const d = await seededDb();
    const q = db.getItem(d, 2)!;
    expect(q.title).toContain("<script>");
    const html = itemHtml(q, NOW, "N", "vscode-resource:");
    expect(html).not.toContain("<script>alert(1)");
    expect(html).toContain("body &lt;b&gt;bold&lt;/b&gt; &amp; more");
    const first = itemHtml(db.getItem(d, 1)!, NOW, "N", "vscode-resource:");
    expect(first).toContain(`<span class="tag">urgent</span>`);
    expect(first).toContain(`data-ask="1"`);
  });

  it("gives an open question an answer box that goes back to the session that asked", async () => {
    const d = await seededDb();
    const html = itemHtml(db.getItem(d, 2)!, NOW, "N", "vscode-resource:");
    expect(html).toContain(`<textarea id="answer"`);
    expect(html).toContain(`data-answer="2"`);
    expect(html).toContain("back to the session that asked");
    expect(itemHtml(db.getItem(d, 1)!, NOW, "N", "vscode-resource:")).not.toContain(`data-answer="`);
    const closed = { ...db.getItem(d, 2)!, status: "closed" };
    expect(itemHtml(closed, NOW, "N", "vscode-resource:")).not.toContain(`data-answer="`);
    const orphan = { ...db.getItem(d, 2)!, session_id: "" };
    const o = itemHtml(orphan, NOW, "N", "vscode-resource:");
    expect(o).toContain(`data-answer="2"`);
    expect(o).toContain("no session on record");
  });
});

describe("dashboard queue tab", () => {
  const job = (id: number, status: string, platform: string, host: string, routable = true, priority: number | null = 0) => ({
    id, cycle: "c", project: "core", test_project: "Core.Tests/Core.Tests.csproj", status, tier: "full",
    enqueued_at: "2026-10-19T08:00:00Z", started_at: status === "running" ? "2026-10-19T08:00:00Z" : null,
    ended_at: null, reason: "", platform, host, routable, priority,
  });
  const withQueue = (running: ReturnType<typeof job>[], queued: ReturnType<typeof job>[]): DashboardData =>
    ({ ...data, queue: { running, queued, finished: [], today: [] }, canMove: true });

  it("says where each running job runs", () => {
    const html = dashboardBody(withQueue([job(1, "running", "", "dev"), job(2, "running", "apple", "")], []));
    expect(html).toContain(`data-tab="queue"`);
    expect(html).toMatch(/#1<\/td>.*?on dev/s);
    expect(html).toMatch(/#2<\/td>.*?this machine/s);
  });

  it("offers the route buttons that fit each queued job", () => {
    const html = dashboardBody(withQueue([], [job(3, "queued", "", ""), job(4, "queued", "remote", ""), job(5, "queued", "apple", "")]));
    expect(html).toContain(`data-route="3:remote"`);
    expect(html).not.toContain(`data-route="3:any"`);
    expect(html).toContain(`data-route="4:any"`);
    expect(html).not.toContain(`data-route="4:remote"`);
    expect(html).not.toContain(`data-route="5:remote"`);
    expect(html).not.toContain(`data-route="5:any"`);
    expect(html).toMatch(/#4<\/td>.*?remote only/s);
  });

  it("shows no route buttons on a database from before schema 7", () => {
    const html = dashboardBody(withQueue([], [job(6, "queued", "", "", false)]));
    expect(html).toContain("#6</td>");
    expect(html).not.toContain("data-route=");
  });

  it("gives each running job a live block with the suite's usual duration", () => {
    const d = withQueue([job(1, "running", "", "dev")], []);
    d.suites = [{ project: "core", test_project: "Core.Tests/Core.Tests.csproj", runs: 3, median_s: 1800, last_s: 1700, history: [], mean_lane_wait_s: 0 }];
    const html = dashboardBody(d);
    expect(html).toContain(`data-live="1"`);
    expect(html).toContain(`data-median="1800"`);
  });

  it("offers up and down on queued jobs, none past either end, and marks bumped ones", () => {
    const html = dashboardBody(withQueue([], [job(7, "queued", "", "", true, 1), job(3, "queued", "", ""), job(5, "queued", "", "")]));
    expect(html).not.toContain(`data-move="7:up"`);
    expect(html).toContain(`data-move="7:down"`);
    expect(html).toContain(`data-move="3:up"`);
    expect(html).toContain(`data-move="3:down"`);
    expect(html).toContain(`data-move="5:up"`);
    expect(html).not.toContain(`data-move="5:down"`);
    expect(html).toMatch(/#7<\/td>.*?bumped.*?#3<\/td>/s);
    expect(html.match(/>bumped</g)).toHaveLength(1);
  });

  it("offers no moves on a database from before schema 6", () => {
    const html = dashboardBody(withQueue([], [job(6, "queued", "", "", false, null), job(8, "queued", "", "", false, null)]));
    expect(html).not.toContain("data-move=");
  });

  it("offers no moves when the brain it runs has no queue move", () => {
    const d = withQueue([], [job(7, "queued", "", "", true, 0), job(3, "queued", "", "", true, 0)]);
    expect(dashboardBody({ ...d, canMove: false })).not.toContain("data-move=");
    expect(dashboardBody({ ...d, canMove: undefined })).not.toContain("data-move=");
  });

  it("says when nothing is queued or running", () => {
    expect(dashboardBody(withQueue([], []))).toContain("Nothing queued or running.");
  });
});

describe("dashboard lanes", () => {
  it("shows a card per lane with its running job's live bar", () => {
    const d: DashboardData = { ...data, remotes: [{ name: "dev", host: "dev", cpus: "2", memory: "6g" }] };
    d.queue = { running: [{ id: 9, cycle: "", project: "p", test_project: "A/A.Tests.csproj", status: "running", tier: "full",
      enqueued_at: "", started_at: null, ended_at: null, reason: "", platform: "", host: "dev", routable: true }],
      queued: [], finished: [], today: [] };
    const html = dashboardHtml(d, "n", "c");
    expect(html).toContain("this machine");
    expect(html).toContain('data-lane-live="9"');
    expect(html).toMatch(/dev[\s\S]*2 cpus, 6g/);
    expect(html).toContain("idle");
  });

  it("keeps a long last-test name inside its lane card", () => {
    const html = dashboardHtml(data, "n", "c");
    // Grid items default to min-width: auto, so an unbroken test name widens the card past its column.
    expect(html).toMatch(/\.lanes > \.lane, \.lane > \* \{ min-width: 0; \}/);
    expect(html).toMatch(/\.last \{[^}]*overflow-wrap: anywhere/);
    expect(html).toContain(`'<span class="muted last">last: '`);
  });
});

describe("icons", () => {
  it("names only icons the codicon font has", () => {
    const css = readFileSync(join(__dirname, "../node_modules/@vscode/codicons/dist/codicon.css"), "utf8");
    const d: DashboardData = { ...data, remotes: [{ name: "dev", host: "dev", cpus: "2", memory: "6g" }] };
    const job = (id: number, status: string, platform: string, host: string) => ({ id, cycle: "", project: "p", test_project: "A/A.Tests.csproj",
      status, tier: "full", enqueued_at: "", started_at: null, ended_at: null, reason: "", platform, host, routable: true });
    d.queue = { running: [job(1, "running", "", ""), job(2, "running", "", "dev")],
      queued: [job(3, "queued", "", ""), job(4, "queued", "remote", ""), job(5, "queued", "apple", "")], finished: [], today: [] };
    const html = dashboardHtml(d, "n", "c");
    const names = new Set([...html.matchAll(/codicon-([a-z][a-z0-9-]*)/g)].map((m) => m[1]));
    // The webview script builds its pills from string literals: pill(text, 'tone', 'icon').
    for (const m of html.matchAll(/pill\([^()]*?, '[a-z]+', '([a-z0-9-]+)'\)/g)) names.add(m[1]);
    names.delete("modifier-spin");
    const missing = [...names].filter((n) => !css.includes(`.codicon-${n}:before`));
    expect(missing).toEqual([]);
  });
});

describe("webview script", () => {
  it("parses", () => {
    const html = dashboardHtml(data, "n", "c");
    const js = html.split('<script nonce="n">')[1].split("</script>")[0];
    expect(() => new Function(js)).not.toThrow();
  });
});
