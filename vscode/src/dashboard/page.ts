import { esc } from "./escape";

export const STYLE = `
  body { font-family: var(--vscode-font-family); font-size: var(--vscode-font-size); color: var(--vscode-foreground);
         background: var(--vscode-editor-background); padding: 12px 20px; }
  h1 { font-size: 1.4em; margin: 0 0 4px; } h2 { font-size: 1.1em; margin: 22px 0 6px; border-bottom: 1px solid var(--vscode-editorWidget-border, #8884); padding-bottom: 3px; }
  table { border-collapse: collapse; width: 100%; } th, td { text-align: left; padding: 3px 10px 3px 0; vertical-align: middle; }
  th { color: var(--vscode-descriptionForeground); font-weight: 600; } td.num, th.num { text-align: right; }
  .muted { color: var(--vscode-descriptionForeground); } .note { color: var(--vscode-editorWarning-foreground); }
  .tiles { display: flex; flex-wrap: wrap; gap: 10px; } .tile { border: 1px solid var(--vscode-editorWidget-border, #8884); border-radius: 4px; padding: 6px 12px; min-width: 110px; }
  .tile b { display: block; font-size: 1.6em; } .tile.bad b { color: var(--vscode-charts-red); } .tile.warn b { color: var(--vscode-charts-yellow); }
  .tag { border: 1px solid var(--vscode-editorWidget-border, #8886); border-radius: 3px; padding: 0 5px; font-size: 0.9em; }
  .legend { color: var(--vscode-descriptionForeground); font-size: 0.9em; } .sw { display: inline-block; width: 14px; height: 3px; margin: 0 3px 3px 8px; } .sw.dot { height: 8px; width: 8px; border-radius: 4px; }
  .axis { fill: var(--vscode-descriptionForeground); font-size: 9px; }
  button { background: var(--vscode-button-background); color: var(--vscode-button-foreground); border: 0; padding: 3px 10px; cursor: pointer; border-radius: 2px; }
  button:hover { background: var(--vscode-button-hoverBackground); }
  pre { white-space: pre-wrap; word-break: break-word; background: var(--vscode-textCodeBlock-background); padding: 8px; border-radius: 3px; }
  a { color: var(--vscode-textLink-foreground); cursor: pointer; }
`;

/** One small script: buttons with data-copy copy text, data-open opens an item, data-refresh refreshes. */
export const SCRIPT = `
  const vscode = acquireVsCodeApi();
  document.addEventListener('click', (e) => {
    const el = e.target.closest('[data-copy],[data-open],[data-refresh]');
    if (!el) return;
    if (el.dataset.copy !== undefined) vscode.postMessage({ type: 'copy', text: el.dataset.copy });
    else if (el.dataset.open !== undefined) vscode.postMessage({ type: 'open', id: Number(el.dataset.open) });
    else vscode.postMessage({ type: 'refresh' });
  });
`;

export function page(title: string, body: string, nonce: string, cspSource: string): string {
  const csp = `default-src 'none'; style-src 'nonce-${nonce}' ${cspSource}; script-src 'nonce-${nonce}'; img-src ${cspSource} data:;`;
  return `<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">` +
    `<meta http-equiv="Content-Security-Policy" content="${esc(csp)}">` +
    `<meta name="viewport" content="width=device-width, initial-scale=1"><title>${esc(title)}</title>` +
    `<style nonce="${esc(nonce)}">${STYLE}</style></head><body>${body}` +
    `<script nonce="${esc(nonce)}">${SCRIPT}</script></body></html>`;
}

export function makeNonce(): string {
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  let s = "";
  for (let i = 0; i < 32; i++) s += chars[Math.floor(Math.random() * chars.length)];
  return s;
}
