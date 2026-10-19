import { esc } from "./escape";
import type { Tone } from "../style";

export const STYLE = `
  :root {
    --red: var(--vscode-charts-red, #f14c4c); --orange: var(--vscode-charts-orange, #f5a623);
    --yellow: var(--vscode-charts-yellow, #e2c541); --green: var(--vscode-charts-green, #4fc778);
    --blue: var(--vscode-charts-blue, #3794ff); --purple: var(--vscode-charts-purple, #b180d7);
    --gray: var(--vscode-descriptionForeground, #8a8a8a);
    --fg: var(--vscode-foreground); --bg: var(--vscode-editor-background);
    --card: color-mix(in srgb, var(--bg) 93%, var(--fg) 7%);
    --card2: color-mix(in srgb, var(--bg) 88%, var(--fg) 12%);
    --line: color-mix(in srgb, var(--fg) 14%, transparent);
  }
  * { box-sizing: border-box; }
  body { font-family: var(--vscode-font-family); font-size: var(--vscode-font-size); color: var(--fg); background: var(--bg);
         padding: 16px 22px 40px; margin: 0; }
  .t-red { --tone: var(--red); } .t-orange { --tone: var(--orange); } .t-yellow { --tone: var(--yellow); }
  .t-green { --tone: var(--green); } .t-blue { --tone: var(--blue); } .t-purple { --tone: var(--purple); } .t-gray { --tone: var(--gray); }

  .hero { display: flex; align-items: center; gap: 18px; padding: 16px 20px; border-radius: 12px; color: #fff;
          background: linear-gradient(120deg, #6d3fd6 0%, #3a6ff0 55%, #14b8a6 100%); box-shadow: 0 6px 24px #0004; }
  .hero[class*="t-"] { background: linear-gradient(120deg, color-mix(in srgb, var(--tone) 80%, #000) 0%, color-mix(in srgb, var(--tone) 55%, #6d3fd6) 100%); }
  .hero h1 { font-size: 1.6em; margin: 0; letter-spacing: .3px; display: flex; align-items: center; gap: 8px; }
  .hero h1 .codicon { font-size: 1.1em; }
  .hero .sub { opacity: .85; font-size: .92em; margin-top: 3px; }
  .hero .grow { flex: 1; }
  .hero .donut text { fill: #fff; }
  .hero button { background: #ffffff26; color: #fff; border: 1px solid #ffffff55; }
  .hero button:hover { background: #ffffff40; }
  .kpis { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
  .kpi { background: #ffffff1f; border: 1px solid #ffffff33; border-radius: 999px; padding: 2px 10px; font-size: .9em; }
  .kpi b { font-size: 1.05em; }

  .tabs { display: flex; gap: 6px; flex-wrap: wrap; margin: 16px 0 14px; border-bottom: 1px solid var(--line); padding-bottom: 8px; }
  .tab { background: transparent; color: var(--fg); border: 1px solid transparent; border-radius: 8px; padding: 5px 12px;
         display: inline-flex; align-items: center; gap: 6px; cursor: pointer; font: inherit; opacity: .8; }
  .tab:hover { background: var(--card); opacity: 1; }
  .tab.active { opacity: 1; background: color-mix(in srgb, var(--tone, var(--blue)) 16%, transparent);
                border-color: color-mix(in srgb, var(--tone, var(--blue)) 45%, transparent); }
  .tab .codicon { color: var(--tone, var(--blue)); }
  .badge { background: var(--tone, var(--blue)); color: #fff; border-radius: 999px; padding: 0 7px; font-size: .8em; font-weight: 600; }
  .panel { display: none; animation: fade .18s ease-out; } .panel.active { display: block; }
  @keyframes fade { from { opacity: 0; transform: translateY(3px); } to { opacity: 1; transform: none; } }

  .sec { display: flex; align-items: center; gap: 8px; margin: 18px 0 8px; }
  .sec:first-child { margin-top: 4px; }
  .sec .ico { width: 26px; height: 26px; border-radius: 7px; display: grid; place-items: center;
              background: color-mix(in srgb, var(--tone) 20%, transparent); color: var(--tone); }
  h2 { font-size: 1.1em; margin: 0; } .sec .hint { color: var(--gray); font-size: .9em; }

  .tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 10px; }
  .tile { background: var(--card); border-radius: 10px; padding: 12px 14px; border-left: 4px solid var(--tone);
          display: flex; gap: 12px; align-items: center; }
  .tile .ico { width: 34px; height: 34px; border-radius: 50%; display: grid; place-items: center; flex: none;
               background: color-mix(in srgb, var(--tone) 20%, transparent); color: var(--tone); }
  .tile .ico .codicon { font-size: 18px; }
  .tile b { display: block; font-size: 1.6em; line-height: 1.1; color: var(--tone); }
  .tile span { color: var(--gray); font-size: .9em; }

  .card { background: var(--card); border-radius: 10px; overflow: hidden; border: 1px solid var(--line); }
  .card.pad { padding: 12px 14px; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: 6px 12px; vertical-align: middle; }
  thead th { background: var(--card2); color: var(--gray); font-weight: 600; font-size: .85em; text-transform: uppercase; letter-spacing: .4px; }
  tbody tr:nth-child(even) { background: color-mix(in srgb, var(--fg) 3%, transparent); }
  tbody tr:hover { background: color-mix(in srgb, var(--blue) 12%, transparent); }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  td.zero { color: var(--gray); opacity: .6; }
  td.hot { color: var(--red); font-weight: 600; } td.good { color: var(--green); font-weight: 600; } td.warm { color: var(--orange); font-weight: 600; }
  .muted { color: var(--gray); } td.muted, td.num { white-space: nowrap; }
  .note { color: var(--vscode-editorWarning-foreground, var(--yellow)); background: color-mix(in srgb, var(--yellow) 12%, transparent);
          padding: 6px 10px; border-radius: 6px; }
  .empty { color: var(--gray); padding: 14px; text-align: center; display: flex; gap: 8px; align-items: center; justify-content: center; }
  .empty .codicon { color: var(--green); }

  .tag, .pill { display: inline-flex; align-items: center; gap: 4px; border-radius: 999px; padding: 1px 8px; font-size: .85em; white-space: nowrap; vertical-align: middle; line-height: 18px; margin: 1px 2px 1px 0;
                background: color-mix(in srgb, var(--tone, var(--gray)) 18%, transparent); color: var(--tone, var(--fg));
                border: 1px solid color-mix(in srgb, var(--tone, var(--gray)) 40%, transparent); }
  .count { display: inline-block; min-width: 26px; text-align: center; border-radius: 6px; padding: 1px 6px; font-weight: 600;
           background: color-mix(in srgb, var(--tone) 20%, transparent); color: var(--tone); }

  .legend { color: var(--gray); font-size: 0.9em; margin-top: 6px; display: flex; gap: 14px; flex-wrap: wrap; }
  .sw { display: inline-block; width: 14px; height: 4px; border-radius: 2px; margin-right: 5px; vertical-align: middle; background: var(--tone, var(--gray)); }
  .sw.dot { height: 9px; width: 9px; border-radius: 5px; }
  .axis { fill: var(--gray); font-size: 9px; }
  .grid { stroke: var(--line); stroke-dasharray: 2 3; }

  button { background: var(--vscode-button-background); color: var(--vscode-button-foreground); border: 0; padding: 4px 11px; cursor: pointer;
           border-radius: 6px; font: inherit; display: inline-flex; align-items: center; gap: 5px; }
  button:hover { background: var(--vscode-button-hoverBackground); }
  pre { white-space: pre-wrap; word-break: break-word; background: var(--vscode-textCodeBlock-background, var(--card)); padding: 12px;
        border-radius: 8px; border: 1px solid var(--line); }
  a { color: var(--vscode-textLink-foreground); cursor: pointer; text-decoration: none; } a:hover { text-decoration: underline; }
  .fields th { width: 130px; color: var(--gray); font-weight: 500; background: transparent; text-transform: none; letter-spacing: 0; font-size: 1em; }
`;

/** Buttons with data-copy copy text, data-open opens an item, data-refresh refreshes, data-tab switches tabs. */
export const SCRIPT = `
  const vscode = acquireVsCodeApi();
  function showTab(name) {
    const tabs = document.querySelectorAll('[data-tab]');
    if (!document.querySelector('[data-tab="' + name + '"]')) return;
    tabs.forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
    document.querySelectorAll('[data-panel]').forEach((p) => p.classList.toggle('active', p.dataset.panel === name));
    vscode.setState({ ...(vscode.getState() || {}), tab: name });
  }
  const saved = (vscode.getState() || {}).tab;
  if (saved) showTab(saved);
  document.addEventListener('click', (e) => {
    const el = e.target.closest('[data-copy],[data-open],[data-refresh],[data-tab]');
    if (!el) return;
    if (el.dataset.tab !== undefined) showTab(el.dataset.tab);
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

export function page(title: string, body: string, nonce: string, cspSource: string, codiconCss = ""): string {
  const csp = `default-src 'none'; style-src 'nonce-${nonce}' ${cspSource}; font-src ${cspSource}; script-src 'nonce-${nonce}'; img-src ${cspSource} data:;`;
  return `<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">` +
    `<meta http-equiv="Content-Security-Policy" content="${esc(csp)}">` +
    `<meta name="viewport" content="width=device-width, initial-scale=1"><title>${esc(title)}</title>` +
    (codiconCss ? `<link rel="stylesheet" href="${esc(codiconCss)}">` : "") +
    `<style nonce="${esc(nonce)}">${STYLE}</style></head><body>${body}` +
    `<script nonce="${esc(nonce)}">${SCRIPT}</script></body></html>`;
}

export function makeNonce(): string {
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  let s = "";
  for (let i = 0; i < 32; i++) s += chars[Math.floor(Math.random() * chars.length)];
  return s;
}
