import { describe, expect, it } from "vitest";
import { answerPrompt, answerRoute, askPrompt, canSend, clean, claudeArgs, resolveClaude, validSession, workPrompt } from "../src/claude";

const item = { id: 12, kind: "test-fail", project: "demo", ref: "T.Suite.It", title: "it's broken" };

describe("claude", () => {
  it("names the item and how to read it", () => {
    const p = askPrompt(item);
    expect(p).toContain("#12");
    expect(p).toContain("test-fail");
    expect(p).toContain("T.Suite.It");
    expect(p).toContain("brain show 12");
  });

  it("strips line breaks and control characters from every field", () => {
    const p = askPrompt({ id: 1, kind: "a\nrm -rf ~\n", project: "p\r\x1b[2J", ref: "r\u2028x\x00", title: "t\x07" });
    expect(p).not.toMatch(/[\x00-\x1f\x7f-\x9f\u2028\u2029]/);
    expect(clean("a\n\nb")).toBe("a b");
    expect(clean(null)).toBe("");
  });

  it("sends only pending, risk and question items", () => {
    expect(canSend("pending")).toBe(true);
    expect(canSend("tests-pending")).toBe(true);
    expect(canSend("risk")).toBe(true);
    expect(canSend("open-question")).toBe(true);
    expect(canSend("test-fail")).toBe(false);
    expect(canSend("ruling")).toBe(false);
  });

  it("asks claude to fix pending and risk items and close them", () => {
    const p = workPrompt({ ...item, kind: "risk" });
    expect(p).toContain("brain show 12");
    expect(p).toContain("brain close 12 --resolution fixed");
    expect(p).not.toMatch(/[\x00-\x1f]/);
  });

  it("asks claude to propose an answer to a question and leave it open", () => {
    const p = workPrompt({ ...item, kind: "question" });
    expect(p).toContain("recommend");
    expect(p).not.toContain("brain close");
  });

  it("finds claude only in absolute PATH entries", () => {
    const has = (set: string[]) => (p: string) => set.includes(p);
    expect(resolveClaude("", ".:/opt/bin:/usr/bin", has(["./claude", "/usr/bin/claude"]), "darwin")).toBe("/usr/bin/claude");
    expect(resolveClaude("", "bin:", has(["bin/claude"]), "darwin")).toBeUndefined();
    expect(resolveClaude("./claude", "/usr/bin", () => true, "darwin")).toBeUndefined();
    expect(resolveClaude("/opt/claude", "", has(["/opt/claude"]), "darwin")).toBe("/opt/claude");
  });

  it("refuses Windows cmd and bat shims", () => {
    expect(resolveClaude("C:\\npm\\claude.cmd", "", () => true, "win32")).toBeUndefined();
    expect(resolveClaude("", "C:\\npm", (p) => p === "C:\\npm\\claude.exe", "win32")).toBe("C:\\npm\\claude.exe");
  });

  it("accepts only plain session ids, never one that looks like a flag", () => {
    expect(validSession("0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b")).toBe(true);
    expect(validSession("-x")).toBe(false);
    expect(validSession("a b")).toBe(false);
    expect(validSession("")).toBe(false);
  });

  it("routes an answer so a live session is never started twice", () => {
    expect(answerRoute("0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b", "ended", false)).toBe("resume");
    expect(answerRoute("0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b", "open", true)).toBe("type");
    expect(answerRoute("0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b", "ended", true)).toBe("type");
    expect(answerRoute("0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b", "open", false)).toBe("ask");
    expect(answerRoute("0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b", "unknown", false)).toBe("ask");
    expect(answerRoute("", "unknown", false)).toBe("save");
    expect(answerRoute("-x", "ended", false)).toBe("save");
  });

  it("puts the answer in one clean line", () => {
    const p = answerPrompt({ ...item, kind: "question", title: "which port?" }, "use 8080\nnot 80\x1b[2J");
    expect(p).toContain("#12");
    expect(p).toContain("which port?");
    expect(p).toContain("use 8080 not 80");
    expect(p).not.toMatch(/[\x00-\x1f\x7f-\x9f\u2028\u2029]/);
    expect(p.startsWith("-")).toBe(false);
  });

  it("builds claude arguments with the prompt last", () => {
    expect(claudeArgs({ sessionId: "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b" }, "go")).toEqual(["--session-id", "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b", "go"]);
    expect(claudeArgs({ resume: "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b" }, "go")).toEqual(["--resume", "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b", "go"]);
    expect(claudeArgs({ resume: "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b", fork: true }, "go")).toEqual(["--resume", "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b", "--fork-session", "go"]);
    expect(claudeArgs({ sessionId: "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b" })).toEqual(["--session-id", "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"]);
    expect(() => claudeArgs({ resume: "-p" }, "go")).toThrow();
  });
});
