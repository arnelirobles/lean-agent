import { describe, expect, it } from "vitest";
import { jobLook, kindLook, severityTone, toneColorId } from "../src/style";
import { trendTone } from "../src/dashboard/charts";

describe("looks", () => {
  it("gives each common kind its own icon and tone", () => {
    expect(kindLook("test-fail")).toEqual({ icon: "bug", tone: "red" });
    expect(kindLook("flaky")).toEqual({ icon: "warning", tone: "yellow" });
    expect(kindLook("blocker").tone).toBe("red");
    expect(kindLook("open-question")).toEqual({ icon: "question", tone: "blue" });
    expect(kindLook("running-agent").icon).toBe("hubot");
    expect(kindLook("something-new")).toEqual({ icon: "circle-outline", tone: "gray" });
  });

  it("maps job status and severity to tones", () => {
    expect(jobLook("running").icon).toBe("sync~spin");
    expect(jobLook("failed").tone).toBe("red");
    expect(jobLook("done").tone).toBe("green");
    expect(severityTone("critical")).toBe("red");
    expect(severityTone("")).toBe("gray");
    expect(toneColorId("gray")).toBeUndefined();
    expect(toneColorId("red")).toBe("charts.red");
  });
});

describe("charts", () => {
  it("colors a suite by its last run against the median", () => {
    expect(trendTone([10, 10, 10, 20])).toBe("red");
    expect(trendTone([10, 10, 10, 5])).toBe("green");
    expect(trendTone([10, 11, 10, 10])).toBe("blue");
    expect(trendTone([10])).toBe("blue");
  });
});

describe("narrow panels", () => {
  it("wraps the hero, scrolls wide tables and has a small-screen layout", async () => {
    const { STYLE } = await import("../src/dashboard/page");
    const rule = (sel: string) => STYLE.match(new RegExp(`\\n\\s*${sel.replace(/\./g, "\\.")} \\{([^}]*)\\}`))?.[1] ?? "";
    expect(rule(".hero")).toContain("flex-wrap: wrap");
    expect(rule(".hero .grow")).toMatch(/flex: 1 1 \d+px/);
    expect(rule(".card")).toContain("overflow-x: auto");
    expect(STYLE).toMatch(/@media \(max-width: \d+px\)/);
  });
});
