const MAP: Record<string, string> = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

/** Escape any value for HTML text or a quoted attribute. Every dynamic string goes through this. */
export function esc(value: unknown): string {
  return String(value ?? "").replace(/[&<>"']/g, (c) => MAP[c]);
}
