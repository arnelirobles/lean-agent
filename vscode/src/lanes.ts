import * as fs from "fs";
import * as path from "path";
import type { JobRow, LaneCount, QueueData, RemoteLane } from "./types";

/** One lane as the Queue tab shows it: what it runs now, what it may take next, how its last 24 hours went. */
export interface LaneCard {
  name: string; remote: boolean; host: string; spec: string;
  running?: JobRow; canTake: number; done: number; failed: number;
}

/** The [remote NAME] sections of remotes.ini. [repo NAME] sections and unknown keys are skipped. */
export function parseRemotes(text: string): RemoteLane[] {
  const out: RemoteLane[] = [];
  let cur: RemoteLane | undefined;
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line || line.startsWith("#") || line.startsWith(";")) continue;
    const sec = /^\[remote\s+([^\]]+)\]$/.exec(line);
    if (sec) {
      cur = { name: sec[1].trim(), host: "", cpus: "", memory: "" };
      out.push(cur);
      continue;
    }
    if (line.startsWith("[")) {
      cur = undefined;
      continue;
    }
    const kv = /^([a-z_]+)\s*=\s*(.*)$/.exec(line);
    if (cur && kv && (kv[1] === "host" || kv[1] === "cpus" || kv[1] === "memory")) cur[kv[1]] = kv[2].trim();
  }
  return out;
}

/** The remotes test-phase reads, from the same file. Remote lanes turned off or a missing file give none. */
export function readRemotes(env: NodeJS.ProcessEnv, home: string): RemoteLane[] {
  if (env.TEST_PHASE_REMOTES === "off") return [];
  try {
    return parseRemotes(fs.readFileSync(env.TEST_PHASE_REMOTES_FILE || path.join(home, ".config", "lean", "remotes.ini"), "utf8"));
  } catch {
    return [];
  }
}

export function laneCards(q: QueueData, remotes: RemoteLane[]): LaneCard[] {
  const count = (host: string, status: string) => q.today.filter((c) => c.host === host && c.status === status).reduce((a, c) => a + c.n, 0);
  const local: LaneCard = {
    name: "this machine", remote: false, host: "", spec: "",
    running: q.running.find((j) => !j.host),
    canTake: q.queued.filter((j) => j.platform !== "remote").length,
    done: count("", "done"), failed: count("", "failed"),
  };
  return [local, ...remotes.map((r) => ({
    name: r.name, remote: true, host: r.host,
    spec: [r.cpus && `${r.cpus} cpus`, r.memory].filter(Boolean).join(", "),
    running: q.running.find((j) => j.host === r.name),
    canTake: q.queued.filter((j) => j.platform !== "apple").length,
    done: count(r.name, "done"), failed: count(r.name, "failed"),
  }))];
}

export type { LaneCount };
