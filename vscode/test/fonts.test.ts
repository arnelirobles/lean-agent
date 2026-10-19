import * as fs from "node:fs";
import * as path from "node:path";
import { describe, expect, it } from "vitest";

const FONTS = path.join(__dirname, "..", "media", "fonts");

describe("bundled fonts", () => {
  it("ship with their OFL license text", () => {
    const fonts = fs.readdirSync(FONTS).filter((f) => f.endsWith(".woff2"));
    expect(fonts.length).toBeGreaterThan(0);
    for (const f of fonts) {
      const lic = path.join(FONTS, f.replace(/\.woff2$/, ".OFL.txt"));
      expect(fs.existsSync(lic), `${f} has no license file`).toBe(true);
      expect(fs.readFileSync(lic, "utf8")).toContain("SIL OPEN FONT LICENSE Version 1.1");
    }
  });
});
