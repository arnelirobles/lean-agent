import { esc } from "./escape";
import type { Tone } from "../style";

/** Small inline SVG charts, written by hand. Colors come from the tone variables in page.ts. */

const tv = (tone: Tone) => `var(--${tone})`;

/** Red when the last run is well above the median, green when well below, blue otherwise. */
export function trendTone(values: number[]): Tone {
  if (values.length < 2) return "blue";
  const sorted = [...values].sort((a, b) => a - b);
  const median = sorted[Math.floor(sorted.length / 2)];
  const last = values[values.length - 1];
  if (median > 0 && last > median * 1.2) return "red";
  if (median > 0 && last < median * 0.8) return "green";
  return "blue";
}

export function sparkline(values: number[], w = 120, h = 26): string {
  if (!values.length) return `<span class="muted">no runs</span>`;
  const max = Math.max(...values, 1e-9);
  const step = values.length > 1 ? (w - 6) / (values.length - 1) : 0;
  const pts = values.map((v, i) => [3 + i * step, h - 3 - (v / max) * (h - 6)]);
  const line = pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const [lx, ly] = pts[pts.length - 1];
  const color = tv(trendTone(values));
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="last ${values.length} runs">` +
    `<polyline fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" points="${line}"/>` +
    `<circle cx="${lx.toFixed(1)}" cy="${ly.toFixed(1)}" r="3" fill="${color}"/></svg>`;
}

export function bar(value: number, max: number, w = 140, tone: Tone = "green"): string {
  const fill = max > 0 ? Math.max(0, Math.min(1, value / max)) * w : 0;
  return `<svg class="bar" width="${w}" height="8" viewBox="0 0 ${w} 8" aria-hidden="true">` +
    `<rect width="${w}" height="8" rx="4" fill="var(--track)"/>` +
    `<rect width="${fill.toFixed(1)}" height="8" rx="4" fill="${tv(tone)}"/></svg>`;
}

export interface LinePoint { x: number; start: number | null; end: number | null; stalled: boolean; label: string }

export function loadChart(points: LinePoint[], w = 680, h = 190): string {
  const pad = { l: 34, r: 10, t: 10, b: 18 };
  const vals = points.flatMap((p) => [p.start, p.end]).filter((v): v is number => v != null);
  if (!points.length || !vals.length) return `<p class="empty">No load readings yet.</p>`;
  const maxY = Math.max(...vals, 1);
  const xs = points.map((p) => p.x);
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const sx = (x: number) => pad.l + (maxX === minX ? 0.5 : (x - minX) / (maxX - minX)) * (w - pad.l - pad.r);
  const sy = (y: number) => h - pad.b - (y / maxY) * (h - pad.t - pad.b);
  const series = (pick: (p: LinePoint) => number | null, color: string, fillArea: boolean) => {
    const pts = points.filter((p) => pick(p) != null).map((p) => [sx(p.x), sy(pick(p) as number)]);
    if (!pts.length) return "";
    const line = pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
    let out = "";
    if (fillArea) {
      out += `<polygon fill="var(--area)" opacity=".3" points="${pts[0][0].toFixed(1)},${h - pad.b} ${line} ${pts[pts.length - 1][0].toFixed(1)},${h - pad.b}"/>`;
    }
    return out + `<polyline fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" points="${line}"/>`;
  };
  const grid = [0.25, 0.5, 0.75, 1].map((f) => {
    const y = sy(maxY * f).toFixed(1);
    return `<line class="grid" x1="${pad.l}" y1="${y}" x2="${w - pad.r}" y2="${y}"/>` +
      `<text x="${pad.l - 4}" y="${(Number(y) + 3).toFixed(1)}" text-anchor="end" class="axis">${(maxY * f).toFixed(1)}</text>`;
  }).join("");
  const stalled = points.filter((p) => p.stalled).map((p) => {
    const cx = sx(p.x).toFixed(1), cy = sy(p.end ?? p.start ?? 0).toFixed(1);
    return `<circle cx="${cx}" cy="${cy}" r="8" fill="var(--red)" opacity=".18"/>` +
      `<circle cx="${cx}" cy="${cy}" r="4" fill="var(--red)"><title>${esc(p.label)} stalled</title></circle>`;
  });
  const axis = `<line x1="${pad.l}" y1="${h - pad.b}" x2="${w - pad.r}" y2="${h - pad.b}" stroke="var(--line)"/>` +
    `<text x="${pad.l - 4}" y="${h - pad.b}" text-anchor="end" class="axis">0</text>`;
  return `<svg class="chart" width="100%" viewBox="0 0 ${w} ${h}" role="img" aria-label="machine load per run">${grid}${axis}` +
    series((p) => p.end, "var(--green)", true) + series((p) => p.start, "var(--orange)", false) +
    stalled.join("") + `</svg>` +
    `<div class="legend"><span><span class="sw t-orange"></span>load at start</span>` +
    `<span><span class="sw t-green"></span>load at end</span>` +
    `<span><span class="sw dot t-red"></span>stalled run</span></div>`;
}
