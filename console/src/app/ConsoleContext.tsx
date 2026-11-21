import { createContext, useContext } from "react";
import type { ThemeName } from "../lib/session";

export interface ConsoleContextValue {
  spaceId: string;
  setSpaceId: (spaceId: string) => void;
  theme: ThemeName;
  setTheme: (theme: ThemeName) => void;
  timezone: string;
  setTimezone: (timezone: string) => void;
  /** Incremented when a space is created, archived or activated, so selectors refresh. */
  spacesVersion: number;
  bumpSpaces: () => void;
  signOut: () => void;
}

export const ConsoleContext = createContext<ConsoleContextValue | null>(null);

export function useConsole(): ConsoleContextValue {
  const value = useContext(ConsoleContext);
  if (!value) throw new Error("useConsole must be used inside the console provider");
  return value;
}
