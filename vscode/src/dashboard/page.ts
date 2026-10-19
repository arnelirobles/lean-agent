import { esc } from "./escape";
import type { Tone } from "../style";

/** The baryo.dev house style: flat fills on white, leaf and sun accents. Always white, whatever the VS Code theme, so it stays readable. */
export const STYLE = `
  :root {
    --ink: #1c2321; --body: #4d5552; --muted: #6b736f; --leaf: #0b7a55; --leaf-dark: #075a3e; --mint: #7cc4a4;
    --sun: #ffcd3c; --sun-soft: #ffe08a; --cream: #fff8e8; --sand: #fbf7ee; --line: #ece4d0;
    --tangerine: #f08a24; --rust: #c2461a; --coral: #e8755a; --peach: #f6a99a;
    --bg: #ffffff; --card: #ffffff; --head: #ffffff; --track: var(--line); --area: var(--mint);
    --red: var(--rust); --orange: var(--tangerine); --yellow: #b8860b; --green: var(--leaf); --blue: var(--ink);
    --purple: var(--coral); --gray: var(--muted);
    --shadow: 0 1px 2px rgba(28,35,33,0.04); --lift: 0 14px 32px rgba(28,35,33,0.09);
  }
  @font-face { font-family: "Plus Jakarta Sans"; font-weight: 400 700; font-display: swap; src: url("MEDIA/fonts/plus-jakarta-sans.woff2") format("woff2"); }
  @font-face { font-family: "IBM Plex Mono"; font-weight: 400; font-display: swap; src: url("MEDIA/fonts/ibm-plex-mono.woff2") format("woff2"); }
  * { box-sizing: border-box; }
  body { font-family: "Plus Jakarta Sans", var(--vscode-font-family, system-ui), sans-serif; font-size: 13.5px; line-height: 1.5;
         color: var(--body); background: var(--bg); padding: 20px 26px 48px; margin: 0; }
  code, pre, .mono { font-family: "IBM Plex Mono", var(--vscode-editor-font-family, ui-monospace), monospace; }
  b, h1, h2 { color: var(--ink); }

  .t-red { --tone: var(--red); --fill: var(--rust); --on: #fff; }
  .t-orange { --tone: var(--orange); --fill: var(--tangerine); --on: #1c2321; }
  .t-yellow { --tone: var(--yellow); --fill: var(--sun); --on: #1c2321; }
  .t-green { --tone: var(--green); --fill: var(--leaf); --on: #fff; }
  .t-blue { --tone: var(--blue); --fill: var(--cream); --on: var(--ink); }
  .t-purple { --tone: var(--purple); --fill: var(--peach); --on: #1c2321; }
  .t-gray { --tone: var(--gray); --fill: var(--cream); --on: var(--body); }

  .eyebrow { font-size: 11.5px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; color: var(--rust); }

  .hero { position: relative; overflow: hidden; display: flex; flex-wrap: wrap; align-items: center; gap: 18px; padding: 22px 24px;
          background: #ffffff; border: 1px solid var(--line); border-radius: 20px; box-shadow: var(--shadow); }
  .hero::after { content: ""; position: absolute; right: -30px; top: -30px; width: 150px; height: 150px; border-radius: 50%; opacity: .55;
                 background: repeating-linear-gradient(135deg, var(--stripe, var(--sun)) 0 3px, transparent 3px 7px); pointer-events: none; }
  .hero > * { position: relative; z-index: 1; }
  .hero .fox { width: 52px; height: 52px; flex: none; }
  .hero .mark { width: 52px; height: 52px; flex: none; border-radius: 50%; display: grid; place-items: center; background: var(--fill); color: var(--on); }
  .hero .mark .codicon { font-size: 24px; }
  .hero h1 { font-size: 26px; font-weight: 700; letter-spacing: -0.015em; margin: 2px 0 0; line-height: 1.15; }
  .hero h1 .dot { color: var(--leaf); }
  .hero .sub { color: var(--muted); font-size: 12.5px; margin-top: 4px; }
  .hero .grow { flex: 1 1 260px; min-width: 0; }
  .hero .actions { display: flex; gap: 8px; flex-wrap: wrap; }
  .stats { display: flex; gap: 26px; flex-wrap: wrap; margin-top: 14px; }
  .stat b { display: block; font-size: 28px; font-weight: 700; letter-spacing: -0.02em; line-height: 1; color: var(--leaf); }
  .stat.hot b { color: var(--red); }
  .stat span { font-size: 12px; color: var(--muted); }

  .tabs { display: flex; gap: 4px; flex-wrap: wrap; margin: 18px 0 16px; padding: 5px; background: var(--card);
          border: 1px solid var(--line); border-radius: 999px; width: fit-content; box-shadow: var(--shadow); }
  .tab { background: transparent; color: var(--ink); border: 0; border-radius: 999px; padding: 7px 14px; box-shadow: none;
         display: inline-flex; align-items: center; gap: 7px; cursor: pointer; font: inherit; font-size: 12px; font-weight: 700;
         letter-spacing: .06em; text-transform: uppercase; }
  .tab:hover { background: var(--cream); }
  .tab.active { background: var(--leaf); color: #fff; }
  .badge { background: var(--sun); color: #1c2321; border-radius: 999px; padding: 0 7px; font-size: 11px; font-weight: 700; letter-spacing: 0; }
  .tab.active .badge { background: #fff; color: var(--leaf); }
  .panel { display: none; } .panel.active { display: block; }

  .sec { display: flex; align-items: center; gap: 12px; margin: 26px 0 12px; }
  .panel > .sec:first-child { margin-top: 6px; }
  .sec .ico { width: 34px; height: 34px; border-radius: 50%; flex: none; display: grid; place-items: center; background: var(--fill); color: var(--on); }
  .sec .ico .codicon { font-size: 16px; }
  h2 { font-size: 19px; font-weight: 700; letter-spacing: -0.015em; margin: 0; line-height: 1.2; }

  .tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 12px; }
  .tile { background: var(--card); border: 1px solid var(--line); border-radius: 16px; padding: 16px 18px; box-shadow: var(--shadow);
          display: flex; flex-direction: column; gap: 10px; transition: box-shadow .15s, transform .15s; }
  .tile:hover { box-shadow: var(--lift); transform: translateY(-2px); }
  .tile .ico { width: 32px; height: 32px; border-radius: 50%; display: grid; place-items: center; background: var(--fill); color: var(--on); }
  .tile b { display: block; font-size: 30px; font-weight: 700; letter-spacing: -0.02em; line-height: 1; color: var(--tone); }
  .tile.t-blue b, .tile.t-gray b { color: var(--green); }
  .tile span { color: var(--muted); font-size: 12.5px; }

  .card { background: var(--card); border-radius: 16px; overflow: hidden; overflow-x: auto; border: 1px solid var(--line); box-shadow: var(--shadow); }
  .card.pad { padding: 16px 18px; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: 9px 14px; vertical-align: middle; }
  thead th { background: var(--head); color: var(--muted); font-weight: 700; font-size: 11px; text-transform: uppercase; letter-spacing: .08em;
             border-bottom: 1px solid var(--line); }
  tbody tr + tr td, tbody tr + tr th { border-top: 1px solid var(--line); }
  tbody tr:hover { background: #fdfbf6; }
  td { color: var(--ink); }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  td.zero { color: var(--muted); opacity: .6; }
  td.hot { color: var(--red); font-weight: 700; } td.good { color: var(--green); font-weight: 700; } td.warm { color: var(--orange); font-weight: 700; }
  .muted, td.muted { color: var(--muted); } td.muted, td.num { white-space: nowrap; }
  .note { color: #1c2321; background: var(--sun-soft); padding: 10px 14px; border-radius: 12px; font-weight: 500; }
  .empty { color: var(--muted); padding: 22px; text-align: center; display: flex; gap: 8px; align-items: center; justify-content: center;
           background: var(--card); border: 1px dashed var(--line); border-radius: 16px; margin: 0; }
  .empty .codicon { color: var(--green); }

  .tag { font-family: "Plus Jakarta Sans", sans-serif; display: inline-block; background: var(--cream); color: var(--body); border-radius: 999px; padding: 3px 10px; font-size: 12px;
         font-weight: 700; margin: 1px 2px 1px 0; }
  .pill { display: inline-flex; align-items: center; gap: 5px; border-radius: 999px; padding: 2px 10px; font-size: 11.5px; font-weight: 700;
          white-space: nowrap; vertical-align: middle; line-height: 18px; margin: 1px 2px 1px 0; background: var(--fill, var(--cream)); color: var(--on, var(--ink)); }
  .pill .codicon { font-size: 13px; }
  .t-blue .ico, .t-gray .ico, .t-blue .mark, .t-gray .mark, .t-blue.mark, .t-gray.mark { background: var(--ink); color: var(--bg); }
  .t-blue.pill, .t-gray.pill { box-shadow: inset 0 0 0 1px var(--line); }
  .count { display: inline-block; min-width: 28px; text-align: center; border-radius: 999px; padding: 1px 8px; font-weight: 700;
           background: var(--fill); color: var(--on); }
  .pills { display: flex; flex-wrap: wrap; gap: 4px; margin: 16px 0 4px; }

  .legend { color: var(--muted); font-size: 12px; margin-top: 10px; display: flex; gap: 16px; flex-wrap: wrap; align-items: center; }
  .sw { display: inline-block; width: 16px; height: 4px; border-radius: 2px; margin-right: 6px; vertical-align: middle; background: var(--tone, var(--gray)); }
  .sw.dot { height: 10px; width: 10px; border-radius: 50%; }
  .axis { fill: var(--muted); font-size: 9px; font-family: "IBM Plex Mono", monospace; }
  .grid { stroke: var(--line); }
  .lead { color: var(--body); margin: 0 0 12px; }

  button { font: inherit; font-size: 11.5px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; cursor: pointer;
           border-radius: 999px; padding: 7px 14px; display: inline-flex; align-items: center; gap: 6px; white-space: nowrap;
           background: transparent; color: var(--ink); border: 1.5px solid var(--ink); transition: background .15s, color .15s; }
  button:hover { background: var(--ink); color: var(--bg); }
  button.small { padding: 4px 10px; font-size: 10.5px; border-color: var(--leaf); color: var(--leaf); }
  button.small:hover { background: var(--leaf); color: #fff; }
  button.sun { background: var(--sun); color: #1c2321; border-color: var(--sun); box-shadow: 0 6px 16px rgba(255,205,60,.35); }
  button.sun:hover { background: var(--sun-soft); border-color: var(--sun-soft); }
  button.leaf { background: var(--leaf); color: #fff; border-color: var(--leaf); }
  button.leaf:hover { background: var(--leaf-dark); border-color: var(--leaf-dark); }
  pre { white-space: pre-wrap; word-break: break-word; background: var(--card); color: var(--ink); padding: 16px 18px; margin: 0;
        border-radius: 16px; border: 1px solid var(--line); font-size: 12.5px; }
  a { color: var(--ink); cursor: pointer; text-decoration: none; font-weight: 600; border-bottom: 1.5px solid var(--sun); }
  a:hover { color: var(--leaf); border-color: var(--leaf); }
  .fields th { width: 140px; color: var(--muted); font-weight: 700; font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }
  .fields td { font-family: "IBM Plex Mono", monospace; font-size: 12.5px; }
  .answer textarea { width: 100%; min-height: 96px; box-sizing: border-box; resize: vertical; font: inherit; font-size: 13.5px;
                     color: var(--ink); background: var(--bg); border: 1.5px solid var(--line); border-radius: 12px; padding: 12px 14px; }
  /* A narrow editor column or a phone-sized window. */
  @media (max-width: 560px) {
    .hero { padding: 18px 16px; gap: 14px; border-radius: 16px; }
    .hero::after { width: 100px; height: 100px; }
    .hero .actions { width: 100%; }
    .hero .actions button { flex: 1; justify-content: center; }
    .hero h1 { font-size: 22px; }
    .stats { gap: 14px 22px; }
    .stat b { font-size: 24px; }
    .tabs { width: auto; border-radius: 16px; }
    .tiles { grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); }
    th, td { padding: 8px 10px; }
    .fields th { width: 96px; }
  }
  .live { display: grid; gap: 8px; padding: 4px 0 6px; }
  .live .track, .lane-live .track { height: 10px; border-radius: 6px; background: var(--track); overflow: hidden; }
  .live .fill, .lane-live .fill { height: 100%; border-radius: 6px; background: var(--green); transition: width .6s; }
  .live .fill.over, .lane-live .fill.over { background: var(--orange); }
  .live .counts, .lane .counts { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; font-size: 12.5px; }
  .live pre { font-size: 11.5px; padding: 8px 12px; border-radius: 10px; max-height: 70px; overflow: hidden; }
  .lanes { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; margin-bottom: 8px; }
  .lane { background: var(--card); border: 1px solid var(--line); border-radius: 16px; padding: 14px 16px; box-shadow: var(--shadow); display: grid; gap: 8px; }
  .lane-head { display: flex; align-items: center; gap: 10px; }
  .lane-head .ico { width: 32px; height: 32px; border-radius: 50%; display: grid; place-items: center; background: var(--fill); color: var(--on); }
  .lane-head b { display: block; font-size: 15px; }
  .lane-head .muted { font-size: 12px; }
  .lane-live { display: grid; gap: 6px; }
  .lanes > .lane, .lane > * { min-width: 0; }
  .last { overflow-wrap: anywhere; }
  .lane-live:empty { display: none; }
  tr.live-row td { border-top: none; padding-top: 0; }
  .answer textarea:focus { outline: none; border-color: var(--leaf); }
  .answer .row { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 10px; }
`;

/** Buttons with data-copy copy text, data-open opens an item, data-refresh refreshes, data-tab switches tabs,
 *  data-claude opens Claude Code, data-ask puts an item into it, data-send starts a session that works on it,
 *  data-answer sends the text in #answer as the answer to a question, data-route="ID:to" routes a queued job. */
export const SCRIPT = `
  const vscode = acquireVsCodeApi();
  function showTab(name) {
    const tabs = document.querySelectorAll('[data-tab]');
    if (!document.querySelector('[data-tab="' + name + '"]')) return;
    tabs.forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
    document.querySelectorAll('[data-panel]').forEach((p) => p.classList.toggle('active', p.dataset.panel === name));
    vscode.setState({ ...(vscode.getState() || {}), tab: name });
  }
  function h(s) { return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]); }
  function dur(s) { s = Math.max(0, Math.round(s)); const m = Math.floor(s / 60); return m >= 60 ? Math.floor(m / 60) + 'h ' + (m % 60) + 'm' : m + 'm ' + (s % 60) + 's'; }
  function pill(text, tone, icon) { return '<span class="pill t-' + tone + '"><i class="codicon codicon-' + icon + '" aria-hidden="true"></i>' + h(text) + '</span>'; }
  // Live progress of running jobs, posted by the extension every few seconds.
  window.addEventListener('message', (e) => {
    if (!e.data || e.data.type !== 'live') return;
    for (const j of e.data.jobs) {
      const el = document.querySelector('[data-live="' + j.id + '"]');
      if (!el) continue;
      const median = Number(el.dataset.median) || 0;
      const lane = document.querySelector('[data-lane-live="' + j.id + '"]');
      let bar = '';
      if (median && j.elapsed != null) {
        const pct = Math.min(100, Math.round(100 * j.elapsed / median));
        const left = median - j.elapsed;
        bar = '<div class="track"><div class="fill' + (left < 0 ? ' over' : '') + '" data-pct="' + pct + '"></div></div>' +
          '<span class="muted">' + (left >= 0 ? 'about ' + dur(left) + ' left, usual run ' + dur(median) : dur(-left) + ' past the usual ' + dur(median)) + '</span>';
      }
      const counts = (j.passed ? pill(j.passed + ' passed', 'green', 'pass') : '') +
        (j.failed ? pill(j.failed + ' failed', 'red', 'error') : '') +
        (j.skipped ? pill(j.skipped + ' skipped', 'gray', 'debug-step-over') : '') +
        (!j.passed && !j.failed && !j.skipped ? pill('building or starting', 'blue', 'tools') : '') +
        (j.quietFor != null && j.quietFor > 120 ? pill('log quiet for ' + dur(j.quietFor), 'orange', 'warning') : pill('log active', 'green', 'pulse')) +
        (j.lastTest ? '<span class="muted last">last: ' + h(j.lastTest) + '</span>' : '');
      el.innerHTML = bar + '<div class="counts">' + counts + '</div>' + (j.tail.length ? '<pre>' + h(j.tail.join('\\n')) + '</pre>' : '');
      // The lane card gets the bar and the counts, without the log tail.
      if (lane) lane.innerHTML = bar + '<div class="counts">' + counts + '</div>';
      // Set through the DOM, which the CSP allows, not as a style attribute, which it blocks.
      document.querySelectorAll('.fill[data-pct]').forEach((f) => { f.style.width = f.dataset.pct + '%'; });
    }
  });
  const saved = (vscode.getState() || {}).tab;
  if (saved) showTab(saved);
  document.addEventListener('click', (e) => {
    const el = e.target.closest('[data-copy],[data-open],[data-refresh],[data-tab],[data-claude],[data-ask],[data-send],[data-answer],[data-route]');
    if (!el) return;
    if (el.dataset.tab !== undefined) showTab(el.dataset.tab);
    else if (el.dataset.claude !== undefined) vscode.postMessage({ type: 'claude' });
    else if (el.dataset.answer !== undefined) {
      const box = document.getElementById('answer');
      const text = box ? box.value.trim() : '';
      if (text) vscode.postMessage({ type: 'answer', id: Number(el.dataset.answer), text });
      else if (box) box.focus();
    }
    else if (el.dataset.route !== undefined) {
      const [id, to] = el.dataset.route.split(':');
      vscode.postMessage({ type: 'route', id: Number(id), text: to });
    }
    else if (el.dataset.send !== undefined) vscode.postMessage({ type: 'send', id: Number(el.dataset.send) });
    else if (el.dataset.ask !== undefined) vscode.postMessage({ type: 'ask', id: Number(el.dataset.ask) });
    else if (el.dataset.copy !== undefined) vscode.postMessage({ type: 'copy', text: el.dataset.copy });
    else if (el.dataset.open !== undefined) vscode.postMessage({ type: 'open', id: Number(el.dataset.open) });
    else vscode.postMessage({ type: 'refresh' });
  });
`;

/** A codicon. "sync~spin" spins, like in tree icons. */
export function ic(name: string): string {
  const [base, mod] = name.split("~");
  return `<i class="codicon codicon-${esc(base)}${mod === "spin" ? " codicon-modifier-spin" : ""}" aria-hidden="true"></i>`;
}

export function pill(text: string, tone: Tone, icon?: string): string {
  return `<span class="pill t-${tone}">${icon ? ic(icon) : ""}${esc(text)}</span>`;
}

/** media is the webview URI of the extension's media folder, where the fonts live. */
export function page(title: string, body: string, nonce: string, cspSource: string, codiconCss = "", media = ""): string {
  const csp = `default-src 'none'; style-src 'nonce-${nonce}' ${cspSource}; font-src ${cspSource}; script-src 'nonce-${nonce}'; img-src ${cspSource} data:;`;
  return `<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">` +
    `<meta http-equiv="Content-Security-Policy" content="${esc(csp)}">` +
    `<meta name="viewport" content="width=device-width, initial-scale=1"><title>${esc(title)}</title>` +
    (codiconCss ? `<link rel="stylesheet" href="${esc(codiconCss)}">` : "") +
    `<style nonce="${esc(nonce)}">${STYLE.split("MEDIA").join(media)}</style></head><body>${body}` +
    `<script nonce="${esc(nonce)}">${SCRIPT}</script></body></html>`;
}

export function makeNonce(): string {
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  let s = "";
  for (let i = 0; i < 32; i++) s += chars[Math.floor(Math.random() * chars.length)];
  return s;
}
