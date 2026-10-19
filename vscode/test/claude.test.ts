import { describe, expect, it } from "vitest";
import { askPrompt, claudeLine } from "../src/claude";

const item = { id: 12, kind: "test-fail", project: "demo", ref: "T.Suite.It", title: "it's broken; rm -rf $HOME" };

describe("claude", () => {
  it("names the item and how to read it", () => {
    const p = askPrompt(item);
    expect(p).toContain("#12");
    expect(p).toContain("test-fail");
    expect(p).toContain("T.Suite.It");
    expect(p).not.toContain("\n");
  });

  it("starts claude with the prompt as one quoted argument", () => {
    expect(claudeLine("claude")).toBe("claude");
    expect(claudeLine("claude", "plain")).toBe("claude 'plain'");
    expect(claudeLine("claude", item.title)).toBe(`claude 'it'\\''s broken; rm -rf $HOME'`);
  });
});
