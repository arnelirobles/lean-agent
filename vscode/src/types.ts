export interface ItemRow {
  id: number; project: string; kind: string; ref: string; title: string; severity: string;
  cycle: string; occurrences: number; first_seen: string; last_seen: string; status: string;
}
export interface ItemFull extends ItemRow {
  repo: string; branch: string; body: string; resolution: string; phase: string; source: string; model: string;
  commit_hash: string; link: string; run_id: number | null; closed_at: string | null; tags: string[];
  /** The Claude session that filed it and its folder; empty on items from before schema 5. */
  session_id?: string; session_cwd?: string;
}
export interface JobRow {
  id: number; cycle: string; project: string; test_project: string; status: string; tier: string;
  enqueued_at: string; started_at: string | null; ended_at: string | null; reason: string;
  /** remote: only a remote lane; apple: only this machine; empty: any lane. Both empty before schema 7. */
  platform: string; host: string;
  /** False on a database from before schema 7, where a job cannot be routed. */
  routable: boolean;
  /** Above 0: moved ahead of its place by bump or move. Null on a database from before schema 6, where jobs cannot move. */
  priority?: number | null;
}
export interface Counts { real: number; flaky: number }
/** Jobs a lane finished in the last 24 hours, by status. host is the remote's name, empty for this machine. */
export interface LaneCount { host: string; status: string; n: number }
export interface QueueData { running: JobRow[]; queued: JobRow[]; finished: JobRow[]; today: LaneCount[] }
/** A [remote NAME] section of remotes.ini, as far as the dashboard shows it. */
export interface RemoteLane { name: string; host: string; cpus: string; memory: string }
export interface CycleRow {
  cycle: string; jobs: number; passed: number; failed: number; duration_s: number; stalls: number; flaky_filed: number;
}
export interface SuiteRow {
  project: string; test_project: string; runs: number; median_s: number; last_s: number;
  history: number[]; mean_lane_wait_s: number;
}
export interface LoadPoint { id: number; test_project: string; started: number; load_start: number | null; load_end: number | null; stalled: boolean }
export interface StaleRow { kind: string; n: number; oldest: string }
export interface Headline { realOpen: number; flakyOpen: number; closed7d: number; runs7d: number; testSeconds7d: number }
export interface DashboardData {
  schemaVersion: number | null;
  waiting: ItemRow[];
  headline: Headline;
  cycles: CycleRow[];
  suites: SuiteRow[];
  flakiest: ItemRow[];
  load: LoadPoint[];
  stale: StaleRow[];
  queue: QueueData;
  generatedAt: number;
  /** Remote lanes from remotes.ini; the panel fills it, the database does not know them. */
  remotes?: RemoteLane[];
}
