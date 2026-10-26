import { describe, expect, it } from "vitest";
import { noticeOnce, openPage, PAGE_URL, type Host, type Memento } from "../src/deprecation";

function fakes(pick?: string) {
  const store = new Map<string, unknown>();
  const state: Memento = {
    get: <T>(k: string) => store.get(k) as T | undefined,
    update: async (k, v) => void store.set(k, v),
  };
  const shown: string[] = [];
  const opened: string[] = [];
  const host: Host = {
    showInformationMessage: async (m) => (shown.push(m), pick),
    openExternal: async (u) => void opened.push(u),
  };
  return { state, host, shown, opened };
}

describe("deprecation notice", () => {
  it("shows once per version", async () => {
    const f = fakes();
    await noticeOnce(f.state, "0.11.0", f.host);
    await noticeOnce(f.state, "0.11.0", f.host);
    expect(f.shown).toEqual(["The lean brain panel is deprecated and will be retired. Open the brain page instead."]);
    await noticeOnce(f.state, "0.12.0", f.host);
    expect(f.shown.length).toBe(2);
  });

  it("opens the page only when the button is pressed", async () => {
    const no = fakes();
    await noticeOnce(no.state, "1", no.host);
    expect(no.opened).toEqual([]);
    const yes = fakes("Open brain page");
    await noticeOnce(yes.state, "1", yes.host);
    expect(yes.opened).toEqual(["http://leanagent.localhost:8770"]);
  });

  it("openPage opens the page url", async () => {
    const f = fakes();
    await openPage(f.host);
    expect(f.opened).toEqual([PAGE_URL]);
  });
});
