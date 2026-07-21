import { RunCommandClient, type RunCommandClientOptions } from "./commands";
import { createGameForgeApi } from "./client";
import { readCsrfToken } from "./csrf";
import { queryClient } from "./query-client";

type SessionBoundaryListener = () => void;

const sessionBoundaryListeners = new Set<SessionBoundaryListener>();

export function subscribeSessionBoundary(listener: SessionBoundaryListener): () => void {
  sessionBoundaryListeners.add(listener);
  return () => sessionBoundaryListeners.delete(listener);
}

export function clearAuthorizedSessionState(): void {
  queryClient.clear();
  for (const listener of sessionBoundaryListeners) listener();
}

export const gameForgeApi = createGameForgeApi({
  onSessionBoundary: clearAuthorizedSessionState,
});

type BrowserRunCommandClientOptions = Omit<RunCommandClientOptions, "csrfToken" | "onSessionBoundary">;

export function createBrowserRunCommandClient(
  options: BrowserRunCommandClientOptions = {},
): RunCommandClient {
  return new RunCommandClient({
    ...options,
    csrfToken: readCsrfToken,
    onSessionBoundary: clearAuthorizedSessionState,
  });
}
