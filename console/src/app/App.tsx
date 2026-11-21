import { useCallback, useEffect, useMemo, useState } from "react";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { Layout } from "../components/Layout";
import { ToastProvider } from "../components/ui";
import { clearPendingTokens } from "../lib/pendingTokens";
import {
  clearOwnerToken,
  getOwnerToken,
  onUnauthorized,
  readPrefs,
  writePrefs,
  type ConsolePrefs,
  type ThemeName,
} from "../lib/session";
import { AgentsPage } from "../pages/AgentsPage";
import { ApiPage } from "../pages/ApiPage";
import { BackupPage } from "../pages/BackupPage";
import { ContextLabPage } from "../pages/ContextLabPage";
import { DashboardPage } from "../pages/DashboardPage";
import { GovernancePage } from "../pages/GovernancePage";
import { GraphPage } from "../pages/GraphPage";
import { IntegrationsPage } from "../pages/IntegrationsPage";
import { LoginPage } from "../pages/LoginPage";
import { MemoriesPage } from "../pages/MemoriesPage";
import { MemoryDetailPage } from "../pages/MemoryDetailPage";
import { NotFoundPage } from "../pages/NotFoundPage";
import { OperationsPage } from "../pages/OperationsPage";
import { SettingsPage } from "../pages/SettingsPage";
import { SpacesPage } from "../pages/SpacesPage";
import { ConsoleContext, type ConsoleContextValue } from "./ConsoleContext";

function ConsoleShell({ onSignOut }: { onSignOut: () => void }) {
  const [prefs, setPrefs] = useState<ConsolePrefs>(() => readPrefs());
  const [spacesVersion, setSpacesVersion] = useState(0);

  const applyPrefs = useCallback((next: Partial<ConsolePrefs>) => {
    setPrefs((current) => {
      const merged: ConsolePrefs = { ...current, ...next };
      writePrefs(merged);
      return merged;
    });
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = prefs.theme;
  }, [prefs.theme]);

  const value = useMemo<ConsoleContextValue>(
    () => ({
      spaceId: prefs.space,
      setSpaceId: (spaceId: string) => applyPrefs({ space: spaceId }),
      theme: prefs.theme,
      setTheme: (theme: ThemeName) => applyPrefs({ theme }),
      timezone: prefs.timezone,
      setTimezone: (timezone: string) => applyPrefs({ timezone }),
      spacesVersion,
      bumpSpaces: () => setSpacesVersion((version) => version + 1),
      signOut: onSignOut,
    }),
    [prefs, applyPrefs, spacesVersion, onSignOut],
  );

  return (
    <ConsoleContext.Provider value={value}>
      <BrowserRouter basename="/console">
        <Layout>
          <ErrorBoundary>
            <Routes>
              <Route path="/" element={<DashboardPage />} />
              <Route path="/memories" element={<MemoriesPage />} />
              <Route path="/memories/:memoryId" element={<MemoryDetailPage />} />
              <Route path="/context-lab" element={<ContextLabPage />} />
              <Route path="/graph" element={<GraphPage />} />
              <Route path="/governance" element={<GovernancePage />} />
              <Route path="/spaces" element={<SpacesPage />} />
              <Route path="/agents" element={<AgentsPage />} />
              <Route path="/operations" element={<OperationsPage />} />
              <Route path="/api" element={<ApiPage />} />
              <Route path="/integrations" element={<IntegrationsPage />} />
              <Route path="/backup" element={<BackupPage />} />
              <Route path="/settings" element={<SettingsPage />} />
              <Route path="*" element={<NotFoundPage />} />
            </Routes>
          </ErrorBoundary>
        </Layout>
      </BrowserRouter>
    </ConsoleContext.Provider>
  );
}

export function App() {
  const [signedIn, setSignedIn] = useState<boolean>(() => getOwnerToken() !== null);

  const signOut = useCallback(() => {
    clearOwnerToken();
    clearPendingTokens();
    setSignedIn(false);
  }, []);

  useEffect(() => onUnauthorized(() => {
    clearPendingTokens();
    setSignedIn(false);
  }), []);

  return (
    <ToastProvider>
      {signedIn ? (
        <ConsoleShell onSignOut={signOut} />
      ) : (
        <LoginPage onSignedIn={() => setSignedIn(true)} />
      )}
    </ToastProvider>
  );
}
