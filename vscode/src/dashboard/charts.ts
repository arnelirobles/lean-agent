import { esc } from "./escape";

/** Small inline SVG charts, written by hand. Colors come from VS Code theme variables. */

export function sparkline(values: number[], w = 120, h = 24): string {
  if (!values.length) return `<span class="muted">no runs</span>`;
  const max = Math.max(...values, 1e-9);
  const step = values.length > 1 ? (w - 4) / (values.length - 1) : 0;
  const pts = values.map((v, i) => `${(2 + i * step).toFixed(1)},${(h - 2 - (v / max) * (h - 4)).toFixed(1)}`);
  const last = pts[pts.length - 1].split(",");
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="last ${values.length} runs">` +
    `<polyline fill="none" stroke="var(--vscode-charts-blue)" stroke-width="1.5" points="${pts.join(" ")}"/>` +
    `<circle cx="${last[0]}" cy="${last[1]}" r="2" fill="var(--vscode-charts-blue)"/></svg>`;
}

export function bar(value: number, max: number, w = 120): string {
  const fill = max > 0 ? Math.max(0, Math.min(1, value / max)) * w : 0;
  return `<svg class="bar" width="${w}" height="10" viewBox="0 0 ${w} 10" aria-hidden="true">` +
    `<rect width="${w}" height="10" rx="2" fill="var(--vscode-editorWidget-border, #8884)"/>` +
    `<rect width="${fill.toFixed(1)}" height="10" rx="2" fill="var(--vscode-charts-blue)"/></svg>`;
}

export interface LinePoint { x: number; start: number | null; end: number | null; stalled: boolean; label: string }

export function loadChart(points: LinePoint[], w = 640, h = 160): string {
  const pad = { l: 34, r: 8, t: 8, b: 18 };
  const vals = points.flatMap((p) => [p.start, p.end]).filter((v): v is number => v != null);
  if (!points.length || !vals.length) return `<p class="muted">No load readings yet.</p>`;
  const maxY = Math.max(...vals, 1);
  const xs = points.map((p) => p.x);
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const sx = (x: number) => pad.l + (maxX === minX ? 0.5 : (x - minX) / (maxX - minX)) * (w - pad.l - pad.r);
  const sy = (y: number) => h - pad.b - (y / maxY) * (h - pad.t - pad.b);
  const line = (pick: (p: LinePoint) => number | null, color: string) => {
    const pts = points.filter((p) => pick(p) != null).map((p) => `${sx(p.x).toFixed(1)},${sy(pick(p) as number).toFixed(1)}`);
    return pts.length ? `<polyline fill="none" stroke="${color}" stroke-width="1.5" points="${pts.join(" ")}"/>` : "";
  };
  const stalled = points.filter((p) => p.stalled).map((p) => {
    const y = p.end ?? p.start ?? 0;
    return `<circle cx="${sx(p.x).toFixed(1)}" cy="${sy(y).toFixed(1)}" r="4" fill="var(--vscode-charts-red)"><title>${esc(p.label)} stalled</title></circle>`;
  });
  const axis = `<line x1="${pad.l}" y1="${h - pad.b}" x2="${w - pad.r}" y2="${h - pad.b}" stroke="var(--vscode-editorWidget-border, #8886)"/>` +
    `<line x1="${pad.l}" y1="${pad.t}" x2="${pad.l}" y2="${h - pad.b}" stroke="var(--vscode-editorWidget-border, #8886)"/>` +
    `<text x="${pad.l - 4}" y="${pad.t + 8}" text-anchor="end" class="axis">${maxY.toFixed(1)}</text>` +
    `<text x="${pad.l - 4}" y="${h - pad.b}" text-anchor="end" class="axis">0</text>`;
  return `<svg class="chart" width="100%" viewBox="0 0 ${w} ${h}" role="img" aria-label="machine load per run">${axis}` +
    line((p) => p.start, "var(--vscode-charts-yellow)") + line((p) => p.end, "var(--vscode-charts-orange, var(--vscode-charts-purple))") +
    stalled.join("") + `</svg>` +
    `<div class="legend"><span class="sw" style="background:var(--vscode-charts-yellow)"></span> load at start ` +
    `<span class="sw" style="background:var(--vscode-charts-orange, var(--vscode-charts-purple))"></span> load at end ` +
    `<span class="sw dot" style="background:var(--vscode-charts-red)"></span> stalled run</div>`;
}
