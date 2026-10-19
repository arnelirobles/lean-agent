import { describe, expect, it } from "vitest";
import { jobLook, kindLook, severityTone, toneColorId } from "../src/style";
import { donut, trendTone } from "../src/dashboard/charts";

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

  it("draws one donut arc per non-empty slice", () => {
    const svg = donut([{ value: 3, color: "#f00", label: "real" }, { value: 0, color: "#ff0", label: "flaky" }], "3", "open");
    expect(svg.match(/stroke-dasharray/g)?.length).toBe(1);
    expect(svg).toContain("<title>real: 3</title>");
  });
});
