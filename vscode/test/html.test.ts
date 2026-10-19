import { beforeAll, describe, expect, it } from "vitest";
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
  });
});
