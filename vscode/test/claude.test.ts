import { describe, expect, it } from "vitest";
import { askPrompt, clean } from "../src/claude";

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
});
