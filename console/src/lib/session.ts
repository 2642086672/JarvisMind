/**
 * Browser-side session state.
 *
 * - The owner token lives in ``sessionStorage``: it is gone when the tab closes and
 *   is never written to ``localStorage``. Clearing it is the sign-out.
 * - Preferences (theme, timezone, selected memory space) are UI state in
 *   ``localStorage``. They are not authority: the server re-checks every request.
 */
const TOKEN_KEY = "jarvismind.console.ownerToken";
const PREFS_KEY = "jarvismind.console.prefs";
const DEFAULT_SPACE = "default";

export type ThemeName = "dark" | "light";

export interface ConsolePrefs {
  theme: ThemeName;
  timezone: string;
  space: string;
}

function defaultTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

function storageFor(kind: "session" | "local"): Storage | null {
  try {
    return kind === "session" ? window.sessionStorage : window.localStorage;
  } catch {
    return null;
  }
}

export function getOwnerToken(): string | null {
  const value = storageFor("session")?.getItem(TOKEN_KEY) ?? null;
  return value && value.length > 0 ? value : null;
}

export function setOwnerToken(token: string): void {
  storageFor("session")?.setItem(TOKEN_KEY, token);
}

export function clearOwnerToken(): void {
  storageFor("session")?.removeItem(TOKEN_KEY);
}

export function readPrefs(): ConsolePrefs {
  const fallback: ConsolePrefs = { theme: "dark", timezone: defaultTimezone(), space: DEFAULT_SPACE };
  const raw = storageFor("local")?.getItem(PREFS_KEY);
  if (!raw) return fallback;
  try {
    const parsed = JSON.parse(raw) as Partial<ConsolePrefs>;
    return {
      theme: parsed.theme === "light" ? "light" : "dark",
      timezone: typeof parsed.timezone === "string" && parsed.timezone ? parsed.timezone : fallback.timezone,
      space: typeof parsed.space === "string" && parsed.space ? parsed.space : DEFAULT_SPACE,
    };
  } catch {
    return fallback;
  }
}

export function writePrefs(next: ConsolePrefs): void {
  storageFor("local")?.setItem(PREFS_KEY, JSON.stringify(next));
}

export function getActiveSpace(): string {
  return readPrefs().space;
}

type UnauthorizedListener = () => void;
const unauthorizedListeners = new Set<UnauthorizedListener>();

export function onUnauthorized(listener: UnauthorizedListener): () => void {
  unauthorizedListeners.add(listener);
  return () => {
    unauthorizedListeners.delete(listener);
  };
}

/** Called by the API client when the server rejects the owner credential. */
export function notifyUnauthorized(): void {
  clearOwnerToken();
  for (const listener of unauthorizedListeners) listener();
}
