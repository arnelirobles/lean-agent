import { describe, expect, it } from "vitest";
import { laneCards, parseRemotes } from "../src/lanes";
import type { JobRow, QueueData } from "../src/types";

const job = (id: number, platform = "", host = ""): JobRow => ({
  id, cycle: "", project: "p", test_project: `T${id}.csproj`, status: "queued", tier: "full", enqueued_at: "",
  started_at: null, ended_at: null, reason: "", platform, host, routable: true,
});

describe("parseRemotes", () => {
  it("reads each remote section and skips repo sections", () => {
    const r = parseRemotes("[remote dev]\nhost = dev\ncpus = 2\nmemory = 6g\n\n[repo app]\ndocker = yes\n");
    expect(r).toEqual([{ name: "dev", host: "dev", cpus: "2", memory: "6g" }]);
  });
});

describe("laneCards", () => {
  it("gives this machine and each remote its job, what it may take next and its last 24 hours", () => {
    const q: QueueData = {
      running: [{ ...job(1, "", "dev"), status: "running" }, { ...job(2), status: "running" }],
      queued: [job(3), job(4, "apple"), job(5, "remote")],
      finished: [],
      today: [{ host: "dev", status: "done", n: 3 }, { host: "", status: "failed", n: 1 }],
    };
    const [local, dev] = laneCards(q, [{ name: "dev", host: "dev", cpus: "2", memory: "6g" }]);
    expect(local).toMatchObject({ name: "this machine", remote: false, canTake: 2, done: 0, failed: 1 });
    expect(local.running?.id).toBe(2);
    expect(dev).toMatchObject({ name: "dev", remote: true, spec: "2 cpus, 6g", canTake: 2, done: 3, failed: 0 });
    expect(dev.running?.id).toBe(1);
  });

  it("shows a remote with nothing running as idle", () => {
    const [, dev] = laneCards({ running: [], queued: [], finished: [], today: [] }, [{ name: "dev", host: "dev", cpus: "", memory: "" }]);
    expect(dev.running).toBeUndefined();
    expect(dev.spec).toBe("");
  });
});
