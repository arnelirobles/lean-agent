/** Icons and colors per item kind, job status and severity. Shared by the trees and the webviews, no vscode import. */

export type Tone = "red" | "orange" | "yellow" | "green" | "blue" | "purple" | "gray";
export interface Look { icon: string; tone: Tone }

const KIND_RULES: [RegExp, Look][] = [
  [/flaky/, { icon: "warning", tone: "yellow" }],
  [/blocker/, { icon: "circle-slash", tone: "red" }],
  [/fail|bug|^test$/, { icon: "bug", tone: "red" }],
  [/question/, { icon: "question", tone: "blue" }],
  [/pending|to-run|waiting/, { icon: "clock", tone: "orange" }],
  [/ruling|decision|approval/, { icon: "law", tone: "purple" }],
  [/agent|running/, { icon: "hubot", tone: "purple" }],
  [/risk/, { icon: "flame", tone: "orange" }],
  [/finding|minor/, { icon: "search", tone: "yellow" }],
  [/task|ticket/, { icon: "tasklist", tone: "blue" }],
  [/note/, { icon: "note", tone: "gray" }],
];

export function kindLook(kind: string): Look {
  const k = kind.toLowerCase();
  return KIND_RULES.find(([re]) => re.test(k))?.[1] ?? { icon: "circle-outline", tone: "gray" };
}

const JOB_LOOKS: Record<string, Look> = {
  running: { icon: "sync~spin", tone: "blue" },
  queued: { icon: "clock", tone: "gray" },
  done: { icon: "pass-filled", tone: "green" },
  failed: { icon: "error", tone: "red" },
  stalled: { icon: "watch", tone: "orange" },
  cancelled: { icon: "circle-slash", tone: "gray" },
};

export function jobLook(status: string): Look {
  return JOB_LOOKS[status] ?? { icon: "circle-outline", tone: "gray" };
}

export function severityTone(severity: string): Tone {
  return ({ critical: "red", important: "orange", minor: "yellow", info: "blue" } as Record<string, Tone>)[severity] ?? "gray";
}

/** VS Code theme color id for a tone, for ThemeIcon colors in the trees. */
export function toneColorId(tone: Tone): string | undefined {
  return tone === "gray" ? undefined : `charts.${tone}`;
}
