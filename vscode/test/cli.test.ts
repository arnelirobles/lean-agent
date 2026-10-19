import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { describe, expect, it } from "vitest";
import { closeArgs, defaultDbPath, findBrain, reopenArgs, resumeArgs } from "../src/brainCli";

function exe(p: string) {
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, "#!/bin/sh\n");
  fs.chmodSync(p, 0o755);
}

describe("brain cli", () => {
  it("builds argument arrays", () => {
    expect(closeArgs(7, "fixed")).toEqual(["close", "7", "--resolution", "fixed"]);
    expect(closeArgs(7, "not-a-bug")).toEqual(["close", "7", "--wontfix", "--resolution", "not-a-bug"]);
    expect(reopenArgs(7)).toEqual(["reopen", "7"]);
    expect(resumeArgs("p")).toEqual(["resume", "--project", "p"]);
    expect(resumeArgs()).toEqual(["resume"]);
  });

  it("db path: setting, then LEAN_BRAIN_DB, then the default", () => {
    expect(defaultDbPath("/a.db", { LEAN_BRAIN_DB: "/b.db" }, "/h")).toBe("/a.db");
    expect(defaultDbPath("", { LEAN_BRAIN_DB: "/b.db" }, "/h")).toBe("/b.db");
    expect(defaultDbPath("", {}, "/h")).toBe("/h/.local/share/lean/brain.db");
  });

  it("finds brain: setting, then PATH, then the newest plugin", () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), "lb-"));
    const root = path.join(home, ".claude", "plugins", "cache", "lean-agent", "lean-agent");
    exe(path.join(root, "1.9.0", "bin", "brain"));
    exe(path.join(root, "1.10.0", "bin", "brain"));
    exe(path.join(root, "1.2.0", "bin", "brain"));
    expect(findBrain("", { PATH: "" }, home)).toBe(path.join(root, "1.10.0", "bin", "brain"));
    const bin = path.join(home, "bin");
    exe(path.join(bin, "brain"));
    expect(findBrain("", { PATH: bin }, home)).toBe(path.join(bin, "brain"));
    const set = path.join(home, "mine");
    exe(set);
    expect(findBrain(set, { PATH: bin }, home)).toBe(set);
    expect(findBrain(path.join(home, "missing"), { PATH: bin }, home)).toBeUndefined();
    expect(findBrain("", { PATH: "" }, path.join(home, "empty"))).toBeUndefined();
  });
});
