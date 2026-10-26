export const PAGE_URL = "http://leanagent.localhost:8770";
const MESSAGE = "The lean brain panel is deprecated and will be retired. Open the brain page instead.";
const BUTTON = "Open brain page";
const KEY = "leanBrain.deprecationShownFor";

export interface Memento {
  get<T>(key: string): T | undefined;
  update(key: string, value: unknown): PromiseLike<void>;
}

export interface Host {
  showInformationMessage(message: string, ...items: string[]): PromiseLike<string | undefined>;
  openExternal(url: string): PromiseLike<unknown>;
}

export async function openPage(host: Host): Promise<void> {
  await host.openExternal(PAGE_URL);
}

/** Shows the deprecation message once per extension version. */
export async function noticeOnce(state: Memento, version: string, host: Host): Promise<void> {
  if (state.get<string>(KEY) === version) return;
  await state.update(KEY, version);
  const pick = await host.showInformationMessage(MESSAGE, BUTTON);
  if (pick === BUTTON) await openPage(host);
}
